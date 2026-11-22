import json
import os
import time
from datetime import datetime, timezone

import redis

from src.config.settings import REDIS_URL
from src.services.algorithm import run_case_analysis
from src.utils.db import execute, fetch_one, wait_for_db

QUEUE = 'museum_case_anomaly_jobs'
HEARTBEAT = '/shared/worker-heartbeat'
RESUME_INTERVAL = 30


def heartbeat():
    os.makedirs('/shared', exist_ok=True)
    with open(HEARTBEAT, 'w', encoding='utf-8') as handle:
        handle.write(datetime.now(timezone.utc).isoformat())


def resume_dispatch_intents():
    """后端在派单中途退出时，worker 继续把已落库的派单意图成单。"""
    from src.anomaly_grouping import resume_dispatches
    try:
        for ticket_id in resume_dispatches():
            execute(
                "INSERT INTO audit_logs (actor_id, action, entity_type, entity_id, detail) "
                "VALUES (NULL,'resume_dispatch','handling_ticket',%s,'{\"source\":\"worker\"}'::jsonb)",
                (ticket_id,),
            )
    except Exception:
        # 恢复失败不阻塞评分队列，下一次心跳继续尝试。
        pass


def main():
    wait_for_db()
    client = redis.Redis.from_url(REDIS_URL, decode_responses=True)
    last_resume = 0.0
    while True:
        heartbeat()
        now = time.monotonic()
        if now - last_resume >= RESUME_INTERVAL:
            resume_dispatch_intents()
            last_resume = now
        item = client.blpop(QUEUE, timeout=5)
        if not item:
            continue
        _, payload = item
        job = json.loads(payload)
        run_id = int(job['run_id'])
        try:
            execute("UPDATE algorithm_runs SET status='running', updated_at=NOW() WHERE id=%s", (run_id,))
            run_case_analysis(int(job['case_id']), run_id)
        except Exception as exc:
            execute("UPDATE algorithm_runs SET status='failed', error=%s, updated_at=NOW() WHERE id=%s", (str(exc), run_id))
        resume_dispatch_intents()


if __name__ == '__main__':
    main()
