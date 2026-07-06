"""safe_api.py — API 응답 안전 파싱."""

from __future__ import annotations

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def safe_json(response) -> Optional[dict]:
    """requests.Response → dict or None (HTML/빈값/비JSON 시 None 반환)."""
    if response is None:
        return None
    try:
        ct = response.headers.get("Content-Type", "")
        if "html" in ct.lower():
            logger.debug(
                "HTML 응답 수신 (JSON 아님): status=%s text=%s...",
                response.status_code,
                (response.text or "")[:200],
            )
            return None
        text = (response.text or "").strip()
        if not text:
            logger.debug("빈 응답 body: status=%s", response.status_code)
            return None
        return response.json()
    except Exception as exc:
        status = getattr(response, "status_code", "?")
        preview = (response.text or "")[:200]
        logger.debug(
            "JSON 파싱 오류: %s | status=%s | preview=%s", exc, status, preview
        )
        return None
