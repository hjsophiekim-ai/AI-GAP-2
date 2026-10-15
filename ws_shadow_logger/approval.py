"""로거 전용 WebSocket 접속키(approval_key) — POST /oauth2/Approval (KIS 공식 kis_auth.auth_ws 와 같은 형식).

REST 접근토큰 발급 경로는 이 패키지 어디에서도 호출하지 않는다.
캐시는 로거 data dir 의 state/ 아래에만 두고, 같은 날짜면 재사용한다(하루 1회 발급).
"""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from . import config


def cache_path(root: Path, day: str) -> Path:
    return Path(root) / "state" / f"approval_{day}.json"


def load_cached(root: Path, day: str, fingerprint: str):
    p = cache_path(root, day)
    if not p.exists():
        return None
    d = json.loads(p.read_text(encoding="utf-8"))
    return d.get("approval_key") if d.get("fp") == fingerprint else None


def issue(creds: config.Credentials, root: Path, now: datetime, post=None) -> str:
    day = now.strftime("%Y%m%d")
    k = load_cached(root, day, creds.fingerprint)
    if k:
        return k
    url = config.REST_BASE + config.APPROVAL_PATH
    assert url.endswith("/oauth2/Approval")
    if post is None:
        import requests
        post = lambda u, body: requests.post(u, data=json.dumps(body),
                                             headers={"content-type": "application/json; charset=utf-8"}, timeout=10)
    r = post(url, {"grant_type": "client_credentials", "appkey": creds.app_key, "secretkey": creds.app_secret})
    data = r.json()
    key = data.get("approval_key")
    if not key:
        raise RuntimeError(f"approval 발급 실패: status={getattr(r, 'status_code', '?')} {str(data)[:200]}")
    p = cache_path(root, day); p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"approval_key": key, "fp": creds.fingerprint, "issued_at": now.isoformat()}), encoding="utf-8")
    return key
