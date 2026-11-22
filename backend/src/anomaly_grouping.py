"""同厅展柜异常归并、待认领候选与可恢复的认领派单。

归并口径（与 README「同厅异常归并」一致）：

* 同一展厅 ``display_cases.gallery``；
* 同向温湿偏移：温度与湿度相对展柜目标窗口中点的偏移方向一致，且偏移量达到阈值；
* 时段重叠：样本时间差不超过 20 分钟，异常评分接近（极差不超过 0.30）；
* 至少覆盖 2 个不同展柜，才形成一条待认领候选；
* 门开干扰只做观察，不参与归并；
* 样本越过展柜温湿保护范围（目标窗口）的告警不参与归并，保持单柜升级。

认领采用两阶段派单：先写入 ``dispatch_attempts`` 意图（含领单时的成员快照），
再在独立事务内成单。进程中断后，启动、worker 心跳和接口访问都会恢复未完成意图；
``dispatch_key`` 唯一约束与 ``handling_ticket_members.event_id`` 唯一约束保证
重试不会重复开单、同一告警只进一张处置单。
"""

import hashlib
import json
from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal
from threading import Lock

import psycopg2
import psycopg2.extras
from fastapi import HTTPException

from src.utils.db import get_conn, query

GROUP_WINDOW = timedelta(minutes=20)
SCORE_BAND = 0.30
MIN_TEMP_DELTA = 0.2
MIN_RH_DELTA = 0.5
MIN_CASES = 2

DIRECTION_LABELS = {
    'temp_up_rh_up': '温度上升 · 湿度上升',
    'temp_up_rh_down': '温度上升 · 湿度下降',
    'temp_down_rh_up': '温度下降 · 湿度上升',
    'temp_down_rh_down': '温度下降 · 湿度下降',
}

_schema_lock = Lock()
_schema_ready = False

_SCHEMA_STATEMENTS = (
    'ALTER TABLE anomaly_events ADD COLUMN IF NOT EXISTS escalation_basis VARCHAR(40)',
    '''DELETE FROM anomaly_events a USING anomaly_events b
       WHERE a.sample_id = b.sample_id AND a.id > b.id''',
    'CREATE UNIQUE INDEX IF NOT EXISTS uniq_anomaly_events_sample ON anomaly_events(sample_id)',
    '''CREATE TABLE IF NOT EXISTS anomaly_candidates (
        id SERIAL PRIMARY KEY,
        gallery VARCHAR(120) NOT NULL,
        signature CHAR(64) UNIQUE NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (status IN ('pending','claimed','superseded')),
        revision INTEGER NOT NULL DEFAULT 1,
        score_min NUMERIC(8,5) NOT NULL,
        score_max NUMERIC(8,5) NOT NULL,
        window_start TIMESTAMPTZ NOT NULL,
        window_end TIMESTAMPTZ NOT NULL,
        direction VARCHAR(20) NOT NULL,
        member_count INTEGER NOT NULL DEFAULT 0,
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_anomaly_candidates_gallery
        ON anomaly_candidates(gallery, status, window_start DESC)''',
    '''CREATE TABLE IF NOT EXISTS anomaly_candidate_members (
        event_id INTEGER PRIMARY KEY REFERENCES anomaly_events(id) ON DELETE CASCADE,
        candidate_id INTEGER NOT NULL REFERENCES anomaly_candidates(id) ON DELETE CASCADE,
        case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_anomaly_candidate_members_candidate
        ON anomaly_candidate_members(candidate_id)''',
    '''CREATE TABLE IF NOT EXISTS handling_tickets (
        id SERIAL PRIMARY KEY,
        dispatch_key VARCHAR(120) UNIQUE NOT NULL,
        candidate_id INTEGER REFERENCES anomaly_candidates(id) ON DELETE SET NULL,
        candidate_revision INTEGER NOT NULL,
        gallery VARCHAR(120) NOT NULL,
        status VARCHAR(20) NOT NULL DEFAULT 'claimed' CHECK (status IN ('claimed','in_progress','closed','cancelled')),
        claimed_by INTEGER NOT NULL REFERENCES users(id),
        claimed_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        member_snapshot JSONB NOT NULL DEFAULT '[]'::jsonb
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_handling_tickets_candidate ON handling_tickets(candidate_id)''',
    '''CREATE INDEX IF NOT EXISTS idx_handling_tickets_gallery ON handling_tickets(gallery, claimed_at DESC)''',
    '''CREATE TABLE IF NOT EXISTS handling_ticket_members (
        ticket_id INTEGER NOT NULL REFERENCES handling_tickets(id) ON DELETE CASCADE,
        event_id INTEGER NOT NULL UNIQUE REFERENCES anomaly_events(id) ON DELETE CASCADE,
        case_id INTEGER NOT NULL REFERENCES display_cases(id) ON DELETE CASCADE,
        PRIMARY KEY (ticket_id, event_id)
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_handling_ticket_members_case ON handling_ticket_members(case_id)''',
    '''CREATE TABLE IF NOT EXISTS dispatch_attempts (
        dispatch_key VARCHAR(120) PRIMARY KEY,
        candidate_id INTEGER NOT NULL REFERENCES anomaly_candidates(id) ON DELETE CASCADE,
        candidate_revision INTEGER NOT NULL,
        state VARCHAR(20) NOT NULL DEFAULT 'pending' CHECK (state IN ('pending','dispatching','completed','failed')),
        attempts INTEGER NOT NULL DEFAULT 1,
        ticket_id INTEGER REFERENCES handling_tickets(id) ON DELETE SET NULL,
        actor_id INTEGER REFERENCES users(id),
        note VARCHAR(500) NOT NULL DEFAULT '',
        snapshot JSONB NOT NULL DEFAULT '[]'::jsonb,
        evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
        last_error TEXT,
        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
    )''',
    '''CREATE INDEX IF NOT EXISTS idx_dispatch_attempts_open
        ON dispatch_attempts(state, updated_at) WHERE state IN ('pending','dispatching')''',
    "ALTER TABLE dispatch_attempts ADD COLUMN IF NOT EXISTS evidence JSONB NOT NULL DEFAULT '{}'::jsonb",
)


def ensure_schema():
    global _schema_ready
    with _schema_lock:
        if _schema_ready:
            return
        # 多个进程可能同时启动；用会话级咨询锁把建表与去重串行化。
        with get_conn() as conn:
            with conn.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(741239002)')
                for statement in _SCHEMA_STATEMENTS:
                    cursor.execute(statement)
        _schema_ready = True


def _json_default(value):
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f'Unsupported type: {type(value)}')


def _json(value):
    return json.dumps(value, default=_json_default, ensure_ascii=False)


def _num(value):
    return float(value) if isinstance(value, Decimal) else value


class DispatchConflict(Exception):
    """派单意图无法成单（并发认领或成员已进入其他处置单）。"""


def _direction(temperature, humidity, temp_mid, rh_mid):
    temp_delta = round(temperature - temp_mid, 2)
    rh_delta = round(humidity - rh_mid, 2)
    if abs(temp_delta) < MIN_TEMP_DELTA or abs(rh_delta) < MIN_RH_DELTA:
        return None, temp_delta, rh_delta
    return (
        f"temp_{'up' if temp_delta > 0 else 'down'}_rh_{'up' if rh_delta > 0 else 'down'}",
        temp_delta,
        rh_delta,
    )


def _eligible_events(cursor):
    cursor.execute(
        '''
        SELECT e.id AS event_id, e.case_id, e.score, e.threshold, e.severity, e.status,
               s.sampled_at, s.temperature, s.humidity, s.co2_ppm,
               c.code AS case_code, c.gallery, c.artifact_title,
               c.target_temp_min, c.target_temp_max, c.target_rh_min, c.target_rh_max,
               (c.target_temp_min + c.target_temp_max) / 2.0 AS temp_mid,
               (c.target_rh_min + c.target_rh_max) / 2.0 AS rh_mid
        FROM anomaly_events e
        JOIN environment_samples s ON s.id = e.sample_id
        JOIN display_cases c ON c.id = e.case_id
        WHERE e.status NOT IN ('closed', 'resolved')
          AND NOT e.door_context
          AND NOT EXISTS (SELECT 1 FROM handling_ticket_members t WHERE t.event_id = e.id)
          -- 派单意图尚未成单时冻结成员，恢复任务仍能按快照成单。
          AND NOT EXISTS (
              SELECT 1 FROM anomaly_candidate_members cm
              JOIN dispatch_attempts d ON d.candidate_id = cm.candidate_id
              WHERE cm.event_id = e.id AND d.state IN ('pending','dispatching'))
          AND NOT (s.temperature < c.target_temp_min OR s.temperature > c.target_temp_max
                   OR s.humidity < c.target_rh_min OR s.humidity > c.target_rh_max)
        ORDER BY c.gallery, s.sampled_at, e.id
        '''
    )
    return [dict(row) for row in cursor.fetchall()]


def _cluster(events):
    """按展厅 + 偏移方向分桶，再按重叠时段与评分极差做贪心聚类。"""
    buckets = defaultdict(list)
    for event in events:
        direction, temp_delta, rh_delta = _direction(
            float(event['temperature']), float(event['humidity']),
            float(event['temp_mid']), float(event['rh_mid']),
        )
        if direction is None:
            continue
        enriched = dict(event)
        enriched['direction'] = direction
        enriched['temp_delta'] = temp_delta
        enriched['rh_delta'] = rh_delta
        buckets[(event['gallery'], direction)].append(enriched)

    clusters = []
    for (gallery, direction), rows in buckets.items():
        rows.sort(key=lambda item: (item['sampled_at'], item['event_id']))
        current = []
        current_start = None
        for row in rows:
            if current:
                span_ok = row['sampled_at'] - current_start <= GROUP_WINDOW
                scores = [float(item['score']) for item in current] + [float(row['score'])]
                band_ok = max(scores) - min(scores) <= SCORE_BAND
            else:
                span_ok = band_ok = True
            if current and (not span_ok or not band_ok):
                clusters.append((gallery, direction, current))
                current = []
            if not current:
                current_start = row['sampled_at']
            current.append(row)
        if current:
            clusters.append((gallery, direction, current))
    return clusters


def _cluster_evidence(gallery, direction, members):
    start = min(item['sampled_at'] for item in members)
    end = max(item['sampled_at'] for item in members)
    details = sorted(
        ({
            'event_id': item['event_id'],
            'case_id': item['case_id'],
            'case_code': item['case_code'],
            'artifact_title': item['artifact_title'],
            'sampled_at': item['sampled_at'],
            'score': _num(item['score']),
            'threshold': _num(item['threshold']),
            'severity': item['severity'],
            'status': item['status'],
            'temperature': _num(item['temperature']),
            'humidity': _num(item['humidity']),
            'co2_ppm': _num(item['co2_ppm']),
            'temp_delta': item['temp_delta'],
            'rh_delta': item['rh_delta'],
            'direction': direction,
        } for item in members),
        key=lambda item: (item['sampled_at'], item['event_id']),
    )
    scores = [float(item['score']) for item in members]
    evidence = {
        'rule': '同厅柜组同向温湿偏移且采样时段重叠，按异常评分接近程度归并为一条待认领候选',
        'gallery': gallery,
        'direction': direction,
        'direction_label': DIRECTION_LABELS[direction],
        'window_minutes': int(GROUP_WINDOW.total_seconds() // 60),
        'score_band': SCORE_BAND,
        'min_temp_delta': MIN_TEMP_DELTA,
        'min_rh_delta': MIN_RH_DELTA,
        'window_start': start,
        'window_end': end,
        'score_min': min(scores),
        'score_max': max(scores),
        'case_count': len({item['case_id'] for item in members}),
        'members': details,
    }
    signature = hashlib.sha256(
        '|'.join([
            gallery,
            direction,
            start.isoformat(),
            ','.join(str(item['event_id']) for item in sorted(members, key=lambda row: row['event_id'])),
        ]).encode('utf-8')
    ).hexdigest()
    return signature, evidence


def _desired_candidates(events):
    desired = {}
    for gallery, direction, members in _cluster(events):
        if len({item['case_id'] for item in members}) < MIN_CASES:
            continue
        signature, evidence = _cluster_evidence(gallery, direction, members)
        desired[signature] = evidence
    return desired



def refresh_candidates():
    """重算未认领候选；已认领候选与处置单成员保持领单时快照。"""
    ensure_schema()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(741239001)')

            # 已进入处置单或仍有未完成派单意图的告警不参与重算，保持领单时快照。
            cursor.execute(
                '''UPDATE anomaly_events e SET escalation_basis=NULL
                   WHERE e.escalation_basis='group_candidate'
                     AND NOT EXISTS (SELECT 1 FROM handling_ticket_members t WHERE t.event_id=e.id)
                     AND NOT EXISTS (
                         SELECT 1 FROM anomaly_candidate_members cm
                         JOIN dispatch_attempts d ON d.candidate_id=cm.candidate_id
                         WHERE cm.event_id=e.id AND d.state IN ('pending','dispatching'))'''
            )
            # 越过展柜温湿保护范围的告警保持单柜升级，不与其他柜归并。
            cursor.execute(
                '''UPDATE anomaly_events e SET escalation_basis='single_case'
                   FROM environment_samples s, display_cases c
                   WHERE s.id=e.sample_id AND c.id=e.case_id
                     AND e.status NOT IN ('closed','resolved')
                     AND NOT e.door_context
                     AND NOT EXISTS (SELECT 1 FROM handling_ticket_members t WHERE t.event_id=e.id)
                     AND NOT EXISTS (
                         SELECT 1 FROM anomaly_candidate_members cm
                         JOIN dispatch_attempts d ON d.candidate_id=cm.candidate_id
                         WHERE cm.event_id=e.id AND d.state IN ('pending','dispatching'))
                     AND (s.temperature < c.target_temp_min OR s.temperature > c.target_temp_max
                          OR s.humidity < c.target_rh_min OR s.humidity > c.target_rh_max)'''
            )

            events = _eligible_events(cursor)
            desired = _desired_candidates(events)

            cursor.execute(
                '''UPDATE anomaly_candidates c SET status='superseded', updated_at=NOW()
                   WHERE c.status='pending'
                     AND NOT EXISTS (SELECT 1 FROM dispatch_attempts d
                                     WHERE d.candidate_id=c.id AND d.state IN ('pending','dispatching'))'''
            )
            cursor.execute(
                '''DELETE FROM anomaly_candidate_members
                   WHERE candidate_id IN (SELECT id FROM anomaly_candidates WHERE status='superseded')'''
            )

            for signature, evidence in desired.items():
                cursor.execute(
                    '''INSERT INTO anomaly_candidates
                           (gallery, signature, status, revision, score_min, score_max,
                            window_start, window_end, direction, member_count, evidence)
                       VALUES (%s,%s,'pending',1,%s,%s,%s,%s,%s,%s,%s::jsonb)
                       ON CONFLICT (signature) DO UPDATE SET
                           status='pending',
                           revision=anomaly_candidates.revision +
                               CASE WHEN anomaly_candidates.evidence IS DISTINCT FROM EXCLUDED.evidence
                                    THEN 1 ELSE 0 END,
                           score_min=EXCLUDED.score_min,
                           score_max=EXCLUDED.score_max,
                           window_start=EXCLUDED.window_start,
                           window_end=EXCLUDED.window_end,
                           direction=EXCLUDED.direction,
                           member_count=EXCLUDED.member_count,
                           evidence=EXCLUDED.evidence,
                           updated_at=NOW()
                       WHERE anomaly_candidates.status IN ('pending','superseded')
                       RETURNING id''',
                    (evidence['gallery'], signature, evidence['score_min'], evidence['score_max'],
                     evidence['window_start'], evidence['window_end'], evidence['direction'],
                     len(evidence['members']), _json(evidence)),
                )
                row = cursor.fetchone()
                if not row:
                    continue
                for member in evidence['members']:
                    cursor.execute(
                        '''INSERT INTO anomaly_candidate_members (event_id, candidate_id, case_id)
                           VALUES (%s,%s,%s) ON CONFLICT (event_id) DO UPDATE
                           SET candidate_id=EXCLUDED.candidate_id, case_id=EXCLUDED.case_id''',
                        (member['event_id'], row['id'], member['case_id']),
                    )

            cursor.execute(
                '''UPDATE anomaly_events e SET escalation_basis='group_candidate'
                   FROM anomaly_candidate_members m
                   JOIN anomaly_candidates c ON c.id=m.candidate_id
                   WHERE m.event_id=e.id AND c.status='pending' '''
            )
    return


def _candidate_rows(cursor, status=None, candidate_id=None):
    sql = '''SELECT * FROM anomaly_candidates'''
    params = []
    conditions = []
    if status:
        conditions.append('status=%s')
        params.append(status)
    if candidate_id is not None:
        conditions.append('id=%s')
        params.append(candidate_id)
    if conditions:
        sql += ' WHERE ' + ' AND '.join(conditions)
    sql += ' ORDER BY window_start DESC, id DESC LIMIT 200'
    cursor.execute(sql, tuple(params))
    return [dict(row) for row in cursor.fetchall()]


def _tickets_for_candidates(cursor, candidate_ids):
    if not candidate_ids:
        return {}
    cursor.execute(
        '''SELECT t.*, u.display_name AS claimed_by_name, u.username AS claimed_by_username
           FROM handling_tickets t JOIN users u ON u.id=t.claimed_by
           WHERE t.candidate_id = ANY(%s) ORDER BY t.claimed_at DESC''',
        (list(candidate_ids),),
    )
    tickets = {}
    for row in cursor.fetchall():
        tickets.setdefault(row['candidate_id'], dict(row))
    return tickets


def _shape_candidate(row, ticket=None):
    evidence = row['evidence'] or {}
    return {
        'id': row['id'],
        'gallery': row['gallery'],
        'status': row['status'],
        'revision': row['revision'],
        'direction': row['direction'],
        'direction_label': DIRECTION_LABELS.get(row['direction'], row['direction']),
        'window_start': row['window_start'],
        'window_end': row['window_end'],
        'score_min': _num(row['score_min']),
        'score_max': _num(row['score_max']),
        'member_count': row['member_count'],
        'evidence': evidence,
        'members': evidence.get('members', []),
        'ticket': ticket,
    }


def list_candidates(status=None):
    refresh_candidates()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            rows = _candidate_rows(cursor, status=status)
            tickets = _tickets_for_candidates(cursor, [row['id'] for row in rows])
            return [_shape_candidate(row, tickets.get(row['id'])) for row in rows]


def candidate_detail(candidate_id):
    refresh_candidates()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            rows = _candidate_rows(cursor, candidate_id=candidate_id)
            if not rows:
                raise HTTPException(404, '待认领候选不存在')
            tickets = _tickets_for_candidates(cursor, [candidate_id])
            return _shape_candidate(rows[0], tickets.get(candidate_id))


def _breaches(temperature, humidity, temp_min, temp_max, rh_min, rh_max):
    labels = []
    if temperature < temp_min:
        labels.append('温度低于保护下限')
    if temperature > temp_max:
        labels.append('温度高于保护上限')
    if humidity < rh_min:
        labels.append('湿度低于保护下限')
    if humidity > rh_max:
        labels.append('湿度高于保护上限')
    return labels


def list_singletons():
    """越过保护范围、按单柜升级的告警。"""
    refresh_candidates()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                '''
                SELECT e.id AS event_id, e.severity, e.status, e.escalation_basis,
                       c.id AS case_id, c.code AS case_code, c.gallery, c.artifact_title,
                       s.sampled_at, s.temperature, s.humidity, s.co2_ppm,
                       c.target_temp_min, c.target_temp_max, c.target_rh_min, c.target_rh_max
                FROM anomaly_events e
                JOIN environment_samples s ON s.id=e.sample_id
                JOIN display_cases c ON c.id=e.case_id
                WHERE e.escalation_basis='single_case' AND e.status NOT IN ('closed','resolved')
                ORDER BY s.sampled_at DESC, e.id DESC LIMIT 200
                '''
            )
            rows = [dict(row) for row in cursor.fetchall()]
    return [{
        'event_id': row['event_id'],
        'severity': row['severity'],
        'status': row['status'],
        'escalation_basis': row['escalation_basis'],
        'case_id': row['case_id'],
        'case_code': row['case_code'],
        'gallery': row['gallery'],
        'artifact_title': row['artifact_title'],
        'sampled_at': row['sampled_at'],
        'temperature': _num(row['temperature']),
        'humidity': _num(row['humidity']),
        'co2_ppm': _num(row['co2_ppm']),
        'breaches': _breaches(
            float(row['temperature']), float(row['humidity']),
            float(row['target_temp_min']), float(row['target_temp_max']),
            float(row['target_rh_min']), float(row['target_rh_max']),
        ),
    } for row in rows]


def _dispatch_key(candidate_id, revision, request_id):
    if request_id:
        return f'request:{request_id}'
    return f'candidate:{candidate_id}:r{revision}'


def _load_members(cursor, candidate_id):
    cursor.execute(
        '''SELECT m.event_id, m.case_id, e.score, e.threshold, e.severity, e.status,
                  s.sampled_at, s.temperature, s.humidity, s.co2_ppm,
                  c.code AS case_code, c.artifact_title, c.gallery
           FROM anomaly_candidate_members m
           JOIN anomaly_events e ON e.id=m.event_id
           JOIN environment_samples s ON s.id=e.sample_id
           JOIN display_cases c ON c.id=m.case_id
           WHERE m.candidate_id=%s ORDER BY s.sampled_at, m.event_id''',
        (candidate_id,),
    )
    return [dict(row) for row in cursor.fetchall()]


def _member_snapshot(members):
    snapshot = []
    for member in members:
        snapshot.append({
            'event_id': member['event_id'],
            'case_id': member['case_id'],
            'case_code': member['case_code'],
            'artifact_title': member['artifact_title'],
            'sampled_at': member['sampled_at'],
            'score': _num(member['score']),
            'severity': member['severity'],
            'temperature': _num(member['temperature']),
            'humidity': _num(member['humidity']),
            'co2_ppm': _num(member['co2_ppm']),
        })
    return snapshot


def _create_attempt(candidate_id, revision, note, user, request_id):
    key = _dispatch_key(candidate_id, revision, request_id)
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            # 与 refresh_candidates 使用同一把事务级咨询锁，避免成员表与候选表交叉加锁。
            cursor.execute('SELECT pg_advisory_xact_lock(741239001)')
            cursor.execute('SELECT * FROM anomaly_candidates WHERE id=%s FOR UPDATE', (candidate_id,))
            candidate = cursor.fetchone()
            if not candidate:
                raise HTTPException(404, '待认领候选不存在')
            cursor.execute(
                '''SELECT t.id, t.dispatch_key FROM handling_tickets t
                   JOIN handling_ticket_members m ON m.ticket_id=t.id
                   WHERE m.event_id IN (SELECT event_id FROM anomaly_candidate_members WHERE candidate_id=%s)
                   LIMIT 1''',
                (candidate_id,),
            )
            conflicting = cursor.fetchone()
            if conflicting and conflicting['dispatch_key'] != key:
                raise HTTPException(409, '候选中的告警已进入其他处置单，请刷新候选')

            cursor.execute('SELECT * FROM dispatch_attempts WHERE dispatch_key=%s FOR UPDATE', (key,))
            attempt = cursor.fetchone()
            if attempt and attempt['state'] in {'completed', 'dispatching'}:
                return key, attempt['ticket_id']

            if candidate['status'] == 'claimed':
                raise HTTPException(409, '候选已被认领，请刷新候选')
            if candidate['status'] != 'pending':
                raise HTTPException(409, '候选已被新的评分结果替换，请刷新后重新认领')
            if candidate['revision'] != revision:
                raise HTTPException(409, '候选成员已有更新，请刷新后重新认领')

            members = _load_members(cursor, candidate_id)
            if not members:
                raise HTTPException(409, '候选没有可派单的告警成员')
            snapshot = _member_snapshot(members)
            # 领单时的成员与成组依据一起固化，后续重算候选或出分不会改写处置单。
            evidence = candidate['evidence'] or {}
            actor_id = int(user['id'])
            if attempt:
                cursor.execute(
                    '''UPDATE dispatch_attempts SET state='pending', attempts=attempts+1,
                              candidate_revision=%s, actor_id=%s, note=%s, snapshot=%s::jsonb,
                              evidence=%s::jsonb, last_error=NULL, updated_at=NOW()
                       WHERE dispatch_key=%s''',
                    (candidate['revision'], actor_id, note, _json(snapshot), _json(evidence), key),
                )
            else:
                cursor.execute(
                    '''INSERT INTO dispatch_attempts
                           (dispatch_key, candidate_id, candidate_revision, state, attempts,
                            actor_id, note, snapshot, evidence)
                       VALUES (%s,%s,%s,'pending',1,%s,%s,%s::jsonb,%s::jsonb)''',
                    (key, candidate_id, candidate['revision'], actor_id, note, _json(snapshot), _json(evidence)),
                )
            return key, None


def complete_dispatch(dispatch_key):
    """把一个已落库的派单意图在独立事务内成单；可安全重复调用。

    同一 dispatch_key 的并发重试共享一张处置单；不同 dispatch_key 争抢同一批告警时，
    由 handling_ticket_members.event_id 的唯一约束裁决，只有一张处置单能拿到告警。
    """
    ensure_schema()
    try:
        with get_conn() as conn:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(741239001)')
                cursor.execute('SELECT * FROM dispatch_attempts WHERE dispatch_key=%s FOR UPDATE', (dispatch_key,))
                attempt = cursor.fetchone()
                if not attempt:
                    raise HTTPException(404, '派单意图不存在')
                if attempt['state'] == 'completed' and attempt['ticket_id']:
                    completed_ticket_id = attempt['ticket_id']
                    attempt = None

                if attempt is not None:
                    cursor.execute('SELECT * FROM handling_tickets WHERE dispatch_key=%s', (dispatch_key,))
                    ticket = cursor.fetchone()
                    if not ticket:
                        cursor.execute('SELECT * FROM anomaly_candidates WHERE id=%s', (attempt['candidate_id'],))
                        candidate = cursor.fetchone()
                        snapshot = attempt['snapshot'] or []
                        event_ids = [member['event_id'] for member in snapshot]
                        if not event_ids:
                            raise DispatchConflict('派单意图没有成员快照')
                        cursor.execute(
                            'SELECT event_id, ticket_id FROM handling_ticket_members WHERE event_id = ANY(%s)',
                            (event_ids,),
                        )
                        taken = cursor.fetchall()
                        if taken:
                            raise DispatchConflict('候选中的告警已进入其他处置单，请刷新候选')
                        cursor.execute(
                            '''INSERT INTO handling_tickets
                                   (dispatch_key, candidate_id, candidate_revision, gallery, status,
                                    claimed_by, evidence, member_snapshot)
                               VALUES (%s,%s,%s,%s,'claimed',%s,%s::jsonb,%s::jsonb)
                               ON CONFLICT (dispatch_key) DO NOTHING RETURNING *''',
                            (dispatch_key, attempt['candidate_id'], attempt['candidate_revision'],
                             (candidate['gallery'] if candidate else None) or
                             (attempt['evidence'] or {}).get('gallery') or '未知展厅',
                             attempt['actor_id'],
                             _json(attempt['evidence'] or (candidate['evidence'] if candidate else {})),
                             _json(snapshot)),
                        )
                        ticket = cursor.fetchone()
                        if not ticket:
                            cursor.execute('SELECT * FROM handling_tickets WHERE dispatch_key=%s', (dispatch_key,))
                            ticket = cursor.fetchone()
                    for member in attempt['snapshot'] or []:
                        cursor.execute(
                            '''INSERT INTO handling_ticket_members (ticket_id, event_id, case_id)
                               VALUES (%s,%s,%s) ON CONFLICT (event_id) DO NOTHING''',
                            (ticket['id'], member['event_id'], member['case_id']),
                        )
                    cursor.execute(
                        '''SELECT event_id, ticket_id FROM handling_ticket_members
                           WHERE event_id = ANY(%s)''',
                        ([member['event_id'] for member in attempt['snapshot'] or []],),
                    )
                    placement = {row['event_id']: row['ticket_id'] for row in cursor.fetchall()}
                    snapshot_ids = {member['event_id'] for member in attempt['snapshot'] or []}
                    if any(placement.get(event_id) != ticket['id'] for event_id in snapshot_ids):
                        raise DispatchConflict('候选中的告警已进入其他处置单，请刷新候选')
                    cursor.execute(
                        '''UPDATE anomaly_candidates SET status='claimed', updated_at=NOW()
                           WHERE id=%s AND status<>'claimed' ''',
                        (attempt['candidate_id'],),
                    )
                    cursor.execute(
                        '''UPDATE dispatch_attempts SET state='completed', ticket_id=%s,
                                  last_error=NULL, updated_at=NOW() WHERE dispatch_key=%s''',
                        (ticket['id'], dispatch_key),
                    )
                    completed_ticket_id = ticket['id']
        return ticket_detail(completed_ticket_id, refresh=False)
    except DispatchConflict as exc:
        query(
            '''UPDATE dispatch_attempts SET state='failed', last_error=%s, updated_at=NOW()
               WHERE dispatch_key=%s AND state<>'completed' ''',
            (str(exc), dispatch_key),
        )
        raise HTTPException(409, str(exc)) from exc


def claim_candidate(candidate_id, revision, note, user, request_id=None):
    key, ticket_id = _create_attempt(candidate_id, revision, note, user, request_id)
    if ticket_id:
        return ticket_detail(ticket_id)
    query(
        '''UPDATE dispatch_attempts SET state='dispatching', updated_at=NOW()
           WHERE dispatch_key=%s AND state='pending' ''',
        (key,),
    )
    return complete_dispatch(key)


def resume_dispatches():
    """恢复中断在「意图已落库、尚未成单」阶段的派单。"""
    ensure_schema()
    pending = [row[0] for row in query(
        "SELECT dispatch_key FROM dispatch_attempts WHERE state IN ('pending','dispatching') "
        'ORDER BY updated_at LIMIT 50'
    )]
    resumed = []
    for key in pending:
        try:
            ticket = complete_dispatch(key)
            resumed.append(ticket['id'])
        except HTTPException:
            continue
    return resumed


def ticket_detail(ticket_id, refresh=True):
    if refresh:
        ensure_schema()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                '''SELECT t.*, u.display_name AS claimed_by_name, u.username AS claimed_by_username
                   FROM handling_tickets t JOIN users u ON u.id=t.claimed_by WHERE t.id=%s''',
                (ticket_id,),
            )
            ticket = cursor.fetchone()
            if not ticket:
                raise HTTPException(404, '处置单不存在')
            cursor.execute(
                '''SELECT m.event_id, m.case_id, c.code AS case_code, c.gallery, c.artifact_title
                   FROM handling_ticket_members m JOIN display_cases c ON c.id=m.case_id
                   WHERE m.ticket_id=%s ORDER BY m.event_id''',
                (ticket_id,),
            )
            members = [dict(row) for row in cursor.fetchall()]
    return {
        'id': ticket['id'],
        'dispatch_key': ticket['dispatch_key'],
        'candidate_id': ticket['candidate_id'],
        'candidate_revision': ticket['candidate_revision'],
        'gallery': ticket['gallery'],
        'status': ticket['status'],
        'claimed_by': ticket['claimed_by'],
        'claimed_by_name': ticket['claimed_by_name'],
        'claimed_by_username': ticket['claimed_by_username'],
        'claimed_at': ticket['claimed_at'],
        'evidence': ticket['evidence'],
        'member_snapshot': ticket['member_snapshot'],
        'members': members,
    }


def list_tickets():
    ensure_schema()
    rows = query(
        '''SELECT t.id, t.gallery, t.status, t.claimed_at, u.display_name,
                  (SELECT COUNT(*) FROM handling_ticket_members m WHERE m.ticket_id=t.id)
           FROM handling_tickets t JOIN users u ON u.id=t.claimed_by
           ORDER BY t.claimed_at DESC LIMIT 100'''
    )
    return [
        {'id': row[0], 'gallery': row[1], 'status': row[2], 'claimed_at': row[3],
         'claimed_by_name': row[4], 'member_count': row[5]}
        for row in rows
    ]




def annotations_for_events(event_ids):
    """Bulk lookup of candidate group and handling ticket for anomaly rows."""
    ids = [int(value) for value in event_ids if value is not None]
    if not ids:
        return {}
    ensure_schema()
    with get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cursor:
            cursor.execute(
                '''SELECT m.event_id, c.id AS candidate_id, c.gallery, c.status AS candidate_status,
                          c.revision AS candidate_revision, c.direction, c.evidence,
                          c.window_start, c.window_end, c.member_count
                   FROM anomaly_candidate_members m JOIN anomaly_candidates c ON c.id=m.candidate_id
                   WHERE m.event_id = ANY(%s)''',
                (ids,),
            )
            candidates = {row['event_id']: dict(row) for row in cursor.fetchall()}
            cursor.execute(
                '''SELECT m.event_id, t.id AS ticket_id, t.status AS ticket_status,
                          t.claimed_at, u.display_name AS claimed_by_name
                   FROM handling_ticket_members m JOIN handling_tickets t ON t.id=m.ticket_id
                   JOIN users u ON u.id=t.claimed_by WHERE m.event_id = ANY(%s)''',
                (ids,),
            )
            tickets = {row['event_id']: dict(row) for row in cursor.fetchall()}
    annotations = {}
    for event_id in ids:
        candidate = candidates.get(event_id)
        ticket = tickets.get(event_id)
        annotations[event_id] = {
            'candidate': None if not candidate else {
                'id': candidate['candidate_id'],
                'gallery': candidate['gallery'],
                'status': candidate['candidate_status'],
                'revision': candidate['candidate_revision'],
                'direction': candidate['direction'],
                'direction_label': DIRECTION_LABELS.get(candidate['direction'], candidate['direction']),
                'window_start': candidate['window_start'],
                'window_end': candidate['window_end'],
                'member_count': candidate['member_count'],
                'basis': (candidate['evidence'] or {}).get('rule', ''),
                'members': (candidate['evidence'] or {}).get('members', []),
            },
            'ticket': None if not ticket else {
                'id': ticket['ticket_id'],
                'status': ticket['ticket_status'],
                'claimed_at': ticket['claimed_at'],
                'claimed_by_name': ticket['claimed_by_name'],
            },
        }
    return annotations
