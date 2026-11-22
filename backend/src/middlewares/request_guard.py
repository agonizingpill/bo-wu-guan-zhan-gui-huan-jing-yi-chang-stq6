import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request

_buckets = defaultdict(deque)


async def rate_limit(request: Request):
    key = request.client.host if request.client else 'unknown'
    now = time.monotonic()
    bucket = _buckets[key]
    while bucket and now - bucket[0] > 60:
        bucket.popleft()
    if len(bucket) >= 120:
        raise HTTPException(status_code=429, detail='操作频率过高，请稍后重试')
    bucket.append(now)
