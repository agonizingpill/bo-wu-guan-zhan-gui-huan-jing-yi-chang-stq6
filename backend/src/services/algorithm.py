from datetime import datetime, timezone
import json

import numpy as np
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

from src.utils.db import clean, execute, execute_returning, fetch_all, fetch_one


RANDOM_STATE = 1069
CONTAMINATION = 0.22


def run_case_analysis(case_id: int, run_id: int):
    from src.anomaly_grouping import ensure_schema as ensure_grouping_schema, refresh_candidates
    ensure_grouping_schema()
    samples = fetch_all(
        '''SELECT s.*, c.target_temp_min, c.target_temp_max, c.target_rh_min, c.target_rh_max
           FROM environment_samples s JOIN display_cases c ON c.id=s.case_id
           WHERE s.case_id=%s AND s.state='accepted' ORDER BY s.sampled_at''',
        (case_id,),
    )
    if len(samples) < 6:
        raise ValueError('至少需要 6 条有效环境采样才能建立异常基线')

    features = np.array([[float(row['temperature']), float(row['humidity']), float(row['co2_ppm'])] for row in samples])
    scaler = RobustScaler()
    scaled = scaler.fit_transform(features)
    model = IsolationForest(contamination=CONTAMINATION, random_state=RANDOM_STATE, n_estimators=160)
    labels = model.fit_predict(scaled)
    scores = model.decision_function(scaled)
    threshold = float(np.quantile(scores, CONTAMINATION))
    previous_flagged = 0
    created_events = []

    for row, label, score in zip(samples, labels, scores):
        abnormal = label == -1
        if abnormal:
            previous_flagged += 1
        else:
            previous_flagged = 0
        if not abnormal:
            continue

        door_context = bool(row['door_open'])
        continuous = previous_flagged >= 3
        anomaly_type = 'door_interference' if door_context else ('continuous_drift' if continuous else 'environment_outlier')
        severity = 'watch' if door_context else ('critical' if continuous else 'high')
        escalated = continuous and not door_context
        status = 'escalated' if escalated else 'open'
        reason = (
            '门磁处于开启段，模型异常只作为开门干扰观察，不判定为设备故障'
            if door_context else
            ('连续异常达到 3 个采样点，已升级馆长查看' if continuous else '环境向量偏离展柜稳态基线')
        )
        existing = fetch_one('SELECT id FROM anomaly_events WHERE sample_id=%s', (row['id'],))
        if existing:
            execute(
                '''UPDATE anomaly_events SET algorithm_run_id=%s, score=%s, threshold=%s,
                   anomaly_type=%s, severity=%s, status=%s, door_context=%s,
                   consecutive_count=%s, escalated_to_curator=%s, reason=%s
                   WHERE id=%s''',
                (run_id, float(score), threshold, anomaly_type, severity, status, door_context, previous_flagged, escalated, reason, existing['id']),
            )
            event_id = existing['id']
        else:
            event = execute_returning(
                '''INSERT INTO anomaly_events
                   (case_id, sample_id, algorithm_run_id, score, threshold, anomaly_type, severity,
                    status, door_context, consecutive_count, escalated_to_curator, reason, created_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL)
                   ON CONFLICT (sample_id) DO UPDATE SET
                       algorithm_run_id=EXCLUDED.algorithm_run_id, score=EXCLUDED.score,
                       threshold=EXCLUDED.threshold, anomaly_type=EXCLUDED.anomaly_type,
                       severity=EXCLUDED.severity, status=EXCLUDED.status,
                       door_context=EXCLUDED.door_context,
                       consecutive_count=EXCLUDED.consecutive_count,
                       escalated_to_curator=EXCLUDED.escalated_to_curator, reason=EXCLUDED.reason
                   RETURNING id''',
                (case_id, row['id'], run_id, float(score), threshold, anomaly_type, severity, status, door_context, previous_flagged, escalated, reason),
            )
            event_id = event['id']
        created_events.append({'event_id': event_id, 'sample_id': row['id'], 'status': status, 'type': anomaly_type, 'score': float(score)})

    metrics = {
        'sample_count': len(samples),
        'outlier_count': len(created_events),
        'outlier_rate': round(len(created_events) / len(samples), 4),
        'threshold': round(threshold, 5),
        'door_interference_count': sum(1 for item in created_events if item['type'] == 'door_interference'),
        'continuous_upgrade_count': sum(1 for item in created_events if item['status'] == 'escalated'),
    }
    result = {
        'events': created_events,
        'feature_order': ['temperature_c', 'relative_humidity_pct', 'co2_ppm'],
        'scaler': 'RobustScaler(median/IQR)',
        'detector': 'IsolationForest(n_estimators=160, contamination=0.22, random_state=1069)',
        'completed_at': datetime.now(timezone.utc).isoformat(),
    }
    execute(
        'UPDATE algorithm_runs SET status=\'succeeded\', metrics=%s::jsonb, result=%s::jsonb, updated_at=NOW() WHERE id=%s',
        (json.dumps(clean(metrics), ensure_ascii=False), json.dumps(clean(result), ensure_ascii=False), run_id),
    )
    # 评分完成后重算同厅候选：新出分的展柜可以并入新的候选，已认领的成员保持处置单快照。
    refresh_candidates()
    return metrics, result
