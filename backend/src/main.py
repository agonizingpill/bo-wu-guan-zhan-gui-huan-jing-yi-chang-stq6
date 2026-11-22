import hashlib
import json
import os
import time
import uuid
from datetime import datetime, timezone

import redis
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .casework_routes import router as operational_cases
from .operations_evidence import router as operations_evidence
from src.config.settings import REDIS_URL
from src.middlewares.auth import current_user, issue_token, require_roles
from src.middlewares.request_guard import rate_limit
from src.utils.db import clean, execute, execute_returning, fetch_all, fetch_one, wait_for_db

app = FastAPI(title='展柜环境异常值守台', version='1.0.0')
app.include_router(operational_cases)
app.include_router(operations_evidence)
redis_client = redis.Redis.from_url(REDIS_URL, decode_responses=True)


class LoginPayload(BaseModel):
    username: str
    password: str


class CasePayload(BaseModel):
    code: str = Field(min_length=3, max_length=40)
    gallery: str
    zone: str
    artifact_code: str
    artifact_title: str
    target_temp_min: float
    target_temp_max: float
    target_rh_min: float
    target_rh_max: float
    door_sensor_id: str


class SamplePayload(BaseModel):
    case_id: int
    sampled_at: datetime
    temperature: float
    humidity: float
    co2_ppm: float
    door_open: bool = False
    note: str = ''


class DutyPayload(BaseModel):
    action_type: str
    notes: str = Field(min_length=2)
    status: str = 'completed'


class ArtifactCheckPayload(BaseModel):
    case_id: int
    event_id: int | None = None
    check_type: str = 'post_incident'
    result: str
    notes: str = Field(min_length=2)


@app.middleware('http')
async def request_middleware(request: Request, call_next):
    started = time.monotonic()
    if request.url.path not in ['/health', '/docs', '/openapi.json']:
        await rate_limit(request)
    try:
        response = await call_next(request)
    except HTTPException:
        raise
    except Exception as exc:
        return JSONResponse(status_code=500, content={'detail': str(exc)})
    response.headers['X-Process-Time'] = f'{time.monotonic() - started:.4f}'
    return response


def password_hash(value: str):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def audit(user, action, entity_type, entity_id=None, detail=None):
    execute(
        'INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail) VALUES (%s,%s,%s,%s,%s::jsonb)',
        (user['id'] if user else None, action, entity_type, entity_id, json.dumps(detail or {}, ensure_ascii=False)),
    )


def get_case_or_404(case_id):
    case = fetch_one('SELECT * FROM display_cases WHERE id=%s', (case_id,))
    if not case:
        raise HTTPException(404, '展柜不存在')
    return case


def get_event_or_404(event_id):
    event = fetch_one(
        '''SELECT e.*, c.code AS case_code, c.artifact_title, s.sampled_at, s.temperature,
                  s.humidity, s.co2_ppm, s.door_open
           FROM anomaly_events e JOIN display_cases c ON c.id=e.case_id
           JOIN environment_samples s ON s.id=e.sample_id WHERE e.id=%s''',
        (event_id,),
    )
    if not event:
        raise HTTPException(404, '异常事件不存在')
    return event


@app.on_event('startup')
def startup():
    wait_for_db()
    if os.getenv('SEED_DEMO', 'true').lower() == 'true':
        from scripts.seed_demo import main as seed_main
        seed_main()


@app.get('/health')
def health():
    db = fetch_one('SELECT 1 AS ok')
    redis_ok = redis_client.ping()
    return {'status': 'healthy', 'db': bool(db and db['ok'] == 1), 'redis': bool(redis_ok)}


@app.post('/login')
def login(payload: LoginPayload):
    user = fetch_one('SELECT id, username, display_name, role, active, password_hash FROM users WHERE username=%s', (payload.username,))
    if not user or not user['active'] or user['password_hash'] != password_hash(payload.password):
        raise HTTPException(401, '用户名或密码不正确')
    audit(user, 'login', 'user', user['id'], {'role': user['role']})
    return {'access_token': issue_token(user), 'token_type': 'bearer', 'user': {key: user[key] for key in ('id', 'username', 'display_name', 'role')}}


@app.get('/me')
def me(user=Depends(current_user)):
    return user


@app.get('/dashboard')
def dashboard(user=Depends(current_user)):
    return {
        'cases': fetch_one("SELECT COUNT(*) AS count FROM display_cases WHERE status='active'")['count'],
        'samples_today': fetch_one("SELECT COUNT(*) AS count FROM environment_samples WHERE sampled_at >= NOW() - INTERVAL '24 hours'")['count'],
        'open_events': fetch_one("SELECT COUNT(*) AS count FROM anomaly_events WHERE status IN ('open','triaged')")['count'],
        'escalated_events': fetch_one("SELECT COUNT(*) AS count FROM anomaly_events WHERE status='escalated'")['count'],
        'door_interference': fetch_one("SELECT COUNT(*) AS count FROM anomaly_events WHERE anomaly_type='door_interference' AND status!='closed'")['count'],
        'latest_run': clean(fetch_one('SELECT * FROM algorithm_runs ORDER BY updated_at DESC LIMIT 1')),
    }


@app.get('/cases')
def cases(user=Depends(current_user)):
    rows = fetch_all(
        '''SELECT c.*, (SELECT COUNT(*) FROM environment_samples s WHERE s.case_id=c.id) AS sample_count,
                  (SELECT COUNT(*) FROM anomaly_events e WHERE e.case_id=c.id AND e.status!='closed') AS active_event_count
           FROM display_cases c ORDER BY c.code'''
    )
    return clean(rows)


@app.post('/cases')
def create_case(payload: CasePayload, user=Depends(require_roles('operator', 'supervisor'))):
    if payload.target_temp_min >= payload.target_temp_max or payload.target_rh_min >= payload.target_rh_max:
        raise HTTPException(422, '展柜环境上下限必须成对递增')
    try:
        row = execute_returning(
            '''INSERT INTO display_cases
               (code, gallery, zone, artifact_code, artifact_title, target_temp_min, target_temp_max,
                target_rh_min, target_rh_max, door_sensor_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *''',
            (payload.code, payload.gallery, payload.zone, payload.artifact_code, payload.artifact_title,
             payload.target_temp_min, payload.target_temp_max, payload.target_rh_min, payload.target_rh_max, payload.door_sensor_id),
        )
    except Exception as exc:
        raise HTTPException(409, f'展柜编号已存在或数据不合法: {exc}') from exc
    audit(user, 'create_case', 'display_case', row['id'], {'code': payload.code})
    return clean(row)


@app.get('/cases/{case_id}')
def case_detail(case_id: int, user=Depends(current_user)):
    case = get_case_or_404(case_id)
    case['samples'] = fetch_all('SELECT * FROM environment_samples WHERE case_id=%s ORDER BY sampled_at DESC LIMIT 30', (case_id,))
    case['events'] = fetch_all(
        '''SELECT e.*, s.sampled_at, s.temperature, s.humidity, s.co2_ppm, s.door_open
           FROM anomaly_events e JOIN environment_samples s ON s.id=e.sample_id
           WHERE e.case_id=%s ORDER BY e.created_at DESC''', (case_id,)
    )
    case['checks'] = fetch_all('SELECT * FROM artifact_checks WHERE case_id=%s ORDER BY checked_at DESC', (case_id,))
    return clean(case)


@app.get('/samples')
def samples(case_id: int | None = None, user=Depends(current_user)):
    query = '''SELECT s.*, c.code AS case_code, c.artifact_title FROM environment_samples s
               JOIN display_cases c ON c.id=s.case_id'''
    params = ()
    if case_id:
        query += ' WHERE s.case_id=%s'
        params = (case_id,)
    query += ' ORDER BY s.sampled_at DESC LIMIT 120'
    return clean(fetch_all(query, params))


@app.post('/samples')
def create_sample(payload: SamplePayload, user=Depends(require_roles('operator', 'supervisor'))):
    case = get_case_or_404(payload.case_id)
    if case['status'] != 'active':
        raise HTTPException(409, '停用或维护中的展柜不能接收采样')
    if not (0 <= payload.humidity <= 100 and -40 <= payload.temperature <= 80 and 0 <= payload.co2_ppm <= 10000):
        raise HTTPException(422, '采样值超出设备可测范围')
    previous = fetch_one('SELECT sampled_at FROM environment_samples WHERE case_id=%s ORDER BY sampled_at DESC LIMIT 1', (payload.case_id,))
    if previous and (payload.sampled_at - previous['sampled_at']).total_seconds() > 30 * 60:
        raise HTTPException(422, '同一展柜采样间隔不能超过 30 分钟，请补齐缺测时段')
    try:
        row = execute_returning(
            '''INSERT INTO environment_samples
               (case_id, sampled_at, temperature, humidity, co2_ppm, door_open, source, state, note, created_by)
               VALUES (%s,%s,%s,%s,%s,%s,'manual','accepted',%s,%s) RETURNING *''',
            (payload.case_id, payload.sampled_at, payload.temperature, payload.humidity, payload.co2_ppm, payload.door_open, payload.note, user['id']),
        )
    except Exception as exc:
        raise HTTPException(409, f'同一时间点已存在采样: {exc}') from exc
    execute('UPDATE display_cases SET last_sample_at=%s WHERE id=%s', (payload.sampled_at, payload.case_id))
    audit(user, 'create_sample', 'environment_sample', row['id'], {'case_id': payload.case_id, 'door_open': payload.door_open})
    return clean(row)


@app.post('/analyze')
def analyze(case_id: int, user=Depends(require_roles('operator', 'supervisor'))):
    get_case_or_404(case_id)
    run_key = f'case-{case_id}-{uuid.uuid4().hex[:12]}'
    row = execute_returning(
        '''INSERT INTO algorithm_runs (run_key, case_id, status, input_summary, parameters)
           VALUES (%s,%s,'queued',%s::jsonb,%s::jsonb) RETURNING *''',
        (run_key, case_id, json.dumps({'case_id': case_id}), json.dumps({'random_state': 1069, 'contamination': 0.22, 'scaler': 'RobustScaler'})),
    )
    redis_client.rpush('museum_case_anomaly_jobs', json.dumps({'run_id': row['id'], 'case_id': case_id}))
    audit(user, 'queue_analysis', 'algorithm_run', row['id'], {'case_id': case_id})
    return clean(row)


@app.get('/runs/{run_id}')
def run_detail(run_id: int, user=Depends(current_user)):
    row = fetch_one('SELECT * FROM algorithm_runs WHERE id=%s', (run_id,))
    if not row:
        raise HTTPException(404, '算法任务不存在')
    return clean(row)


@app.get('/anomalies')
def anomalies(status: str | None = None, user=Depends(current_user)):
    query = '''SELECT e.*, c.code AS case_code, c.artifact_title, s.sampled_at, s.temperature,
                      s.humidity, s.co2_ppm, s.door_open,
                      (SELECT COUNT(*) FROM duty_actions d WHERE d.event_id=e.id AND d.status='completed') AS completed_actions,
                      (SELECT COUNT(*) FROM artifact_checks a WHERE a.event_id=e.id AND a.result='pass') AS passed_checks
               FROM anomaly_events e JOIN display_cases c ON c.id=e.case_id
               JOIN environment_samples s ON s.id=e.sample_id'''
    params = ()
    if status:
        query += ' WHERE e.status=%s'
        params = (status,)
    query += ' ORDER BY e.created_at DESC'
    return clean(fetch_all(query, params))


@app.get('/anomalies/{event_id}')
def anomaly_detail(event_id: int, user=Depends(current_user)):
    event = get_event_or_404(event_id)
    event['actions'] = fetch_all('SELECT d.*, u.display_name AS operator_name FROM duty_actions d JOIN users u ON u.id=d.operator_id WHERE d.event_id=%s ORDER BY d.started_at DESC', (event_id,))
    event['checks'] = fetch_all('SELECT a.*, u.display_name AS inspector_name FROM artifact_checks a JOIN users u ON u.id=a.inspector_id WHERE a.event_id=%s ORDER BY a.checked_at DESC', (event_id,))
    return clean(event)


@app.post('/anomalies/{event_id}/duty-actions')
def duty_action(event_id: int, payload: DutyPayload, user=Depends(require_roles('operator', 'supervisor'))):
    event = get_event_or_404(event_id)
    allowed = {'acknowledge', 'verify_door_sealed', 'adjust_climate', 'inspect_sensor', 'call_curator'}
    if payload.action_type not in allowed:
        raise HTTPException(422, '值守动作类型不受支持')
    if event['status'] == 'closed':
        raise HTTPException(409, '已关闭事件不能追加值守动作')
    if event['door_context'] and payload.action_type != 'verify_door_sealed':
        raise HTTPException(409, '门开干扰事件必须先完成关门复核，不能直接判定设备故障')
    if event['status'] == 'escalated' and payload.action_type in {'acknowledge', 'call_curator'} and user['role'] != 'supervisor':
        raise HTTPException(403, '连续异常已升级，只允许主管确认馆长处置')
    status = payload.status if payload.status in {'pending', 'in_progress', 'completed'} else 'completed'
    completed = datetime.now(timezone.utc) if status == 'completed' else None
    row = execute_returning(
        '''INSERT INTO duty_actions (event_id, action_type, status, notes, operator_id, completed_at)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
        (event_id, payload.action_type, status, payload.notes, user['id'], completed),
    )
    execute("UPDATE anomaly_events SET status=CASE WHEN status='open' THEN 'triaged' ELSE status END WHERE id=%s", (event_id,))
    audit(user, 'duty_action', 'anomaly_event', event_id, {'action_type': payload.action_type, 'status': status})
    return clean(row)


@app.post('/anomalies/{event_id}/artifact-checks')
def artifact_check(event_id: int, payload: ArtifactCheckPayload, user=Depends(require_roles('operator', 'supervisor'))):
    event = get_event_or_404(event_id)
    if payload.case_id != event['case_id']:
        raise HTTPException(422, '巡检展柜与异常事件不一致')
    if payload.result not in {'pass', 'fail', 'pending'}:
        raise HTTPException(422, '巡检结果不受支持')
    row = execute_returning(
        '''INSERT INTO artifact_checks (case_id, event_id, check_type, result, notes, inspector_id)
           VALUES (%s,%s,%s,%s,%s,%s) RETURNING *''',
        (payload.case_id, event_id, payload.check_type, payload.result, payload.notes, user['id']),
    )
    if payload.result == 'fail':
        execute("UPDATE anomaly_events SET status='open', recovery_note=%s WHERE id=%s", ('文物巡检未通过，需要继续值守处置', event_id))
    audit(user, 'artifact_check', 'artifact_check', row['id'], {'event_id': event_id, 'result': payload.result})
    return clean(row)


@app.post('/anomalies/{event_id}/close')
def close_anomaly(event_id: int, user=Depends(require_roles('supervisor'))):
    event = get_event_or_404(event_id)
    if event['status'] == 'closed':
        return clean(event)
    actions = fetch_all("SELECT * FROM duty_actions WHERE event_id=%s AND status='completed'", (event_id,))
    checks = fetch_all("SELECT * FROM artifact_checks WHERE event_id=%s ORDER BY checked_at DESC", (event_id,))
    if not actions:
        raise HTTPException(409, '异常尚未完成值守处置，不能关闭')
    if event['door_context'] and not any(action['action_type'] == 'verify_door_sealed' for action in actions):
        raise HTTPException(409, '门开干扰事件必须完成关门复核后才能关闭')
    if event['status'] == 'escalated' and not any(action['action_type'] == 'call_curator' for action in actions):
        raise HTTPException(409, '连续异常已升级，必须完成馆长处置记录')
    if not checks or checks[0]['result'] != 'pass':
        raise HTTPException(409, '文物巡检尚未通过，不能关闭异常')
    row = execute_returning("UPDATE anomaly_events SET status='closed', resolved_at=NOW() WHERE id=%s RETURNING *", (event_id,))
    audit(user, 'close_anomaly', 'anomaly_event', event_id, {'status': 'closed'})
    return clean(row)


@app.get('/audit')
def audit_list(user=Depends(require_roles('operator', 'supervisor'))):
    return clean(fetch_all('SELECT a.*, u.display_name FROM audit_logs a LEFT JOIN users u ON u.id=a.actor_id ORDER BY a.created_at DESC LIMIT 80'))
