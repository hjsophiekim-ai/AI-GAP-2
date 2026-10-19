"""KIS WebSocket 프레임 파서 (순수 함수).

데이터 프레임: "<암호화 0|1>|<TR_ID>|<건수 NNN>|f1^f2^...^fN[^f1^...]"  (건수 > 1 이면 레코드가 이어 붙는다)
시스템 프레임: JSON {"header": {"tr_id": ..., ...}, "body": {...}}  — 구독 응답, PINGPONG 등.
체결/호가 TR 은 암호화되지 않는다(암호화는 체결통보 계열). 암호화 프레임은 받으면 기록만 하고 파싱하지 않는다.
"""
from __future__ import annotations

import json
from typing import Any

from .schema import ALT_COLUMNS, COLUMNS


class FrameError(ValueError):
    pass


def parse_frame(raw: str) -> dict[str, Any]:
    if not raw:
        raise FrameError("empty frame")
    if raw[0] in ("0", "1"):
        parts = raw.split("|", 3)
        if len(parts) < 4:
            raise FrameError("data frame needs 4 parts")
        enc, tr_id, cnt, body = parts
        if enc == "1":
            return {"type": "encrypted", "tr_id": tr_id}
        cols = COLUMNS.get(tr_id)
        if cols is None:
            return {"type": "unknown_tr", "tr_id": tr_id}
        n = int(cnt)
        vals = body.split("^")
        w = len(cols)
        # 같은 TR 의 전문 폭이 둘인 경우(HDFSASP0 의 RSYM 유무)만 적용된다. 기본 폭과 안 맞고
        # 대체 폭과 정확히 맞을 때만 바꾼다 — 고정폭 슬라이스가 한 칸씩 밀리는 사고(HDFSCNT0
        # RSYM, 2026-10-16)를 TR 추가 때마다 반복하지 않기 위한 장치다.
        alt = ALT_COLUMNS.get(tr_id)
        if alt is not None and n > 0 and len(vals) != w * n and len(vals) == len(alt) * n:
            cols, w = alt, len(alt)
        if len(vals) < w * n:
            raise FrameError(f"{tr_id}: fields {len(vals)} < {w}x{n}")
        recs = [dict(zip(cols, vals[i * w:(i + 1) * w])) for i in range(n)]
        return {"type": "data", "tr_id": tr_id, "records": recs, "width": w}
    try:
        obj = json.loads(raw)
    except json.JSONDecodeError as e:
        raise FrameError(f"bad json: {e}") from e
    hdr = obj.get("header") or {}
    body = obj.get("body") or {}
    return {"type": "pingpong" if hdr.get("tr_id") == "PINGPONG" else "system",
            "tr_id": hdr.get("tr_id"), "tr_key": hdr.get("tr_key"),
            "rt_cd": body.get("rt_cd"), "msg_cd": body.get("msg_cd"), "msg1": body.get("msg1")}


def subscribe_message(approval_key: str, tr_id: str, tr_key: str) -> str:
    """공식 kis_auth.data_fetch 형식, 등록(tr_type "1") 전용. 해제는 하지 않고 세션 종료로 끝낸다."""
    return json.dumps({"header": {"approval_key": approval_key, "custtype": "P",
                                  "tr_type": "1", "content-type": "utf-8"},
                       "body": {"input": {"tr_id": tr_id, "tr_key": tr_key}}})


def num(v, default=None):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default
