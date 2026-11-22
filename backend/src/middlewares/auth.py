from datetime import datetime, timedelta, timezone
from functools import wraps

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from src.config.settings import JWT_ALGORITHM, JWT_SECRET
from src.utils.db import fetch_one

security = HTTPBearer(auto_error=False)


def issue_token(user):
    payload = {
        'sub': str(user['id']),
        'username': user['username'],
        'display_name': user['display_name'],
        'role': user['role'],
        'exp': datetime.now(timezone.utc) + timedelta(hours=8),
    }
    return jwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)


def current_user(credentials: HTTPAuthorizationCredentials = Depends(security)):
    if not credentials:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='需要登录')
    try:
        payload = jwt.decode(credentials.credentials, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='登录凭据已失效') from exc
    user = fetch_one('SELECT id, username, display_name, role, active FROM users WHERE id=%s', (int(payload['sub']),))
    if not user or not user['active']:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail='账号不可用')
    return user


def require_roles(*roles):
    def dependency(user=Depends(current_user)):
        if user['role'] not in roles:
            raise HTTPException(status_code=403, detail=f'需要角色: {", ".join(roles)}')
        return user
    return dependency
