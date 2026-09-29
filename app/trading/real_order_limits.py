"""실계좌 주문금액 사용자 한도 — UI 에서 명시적으로 설정한 값만 저장한다 (2026-09-29).

배경 (2026-09-29 09:54 실사고): P3/N1 프리셋이 SMART 사이징을 강제로 켜서
1번 슬롯 주문이 예산 1,000만원 x1.05 = 10,442,695원이 됐는데, config.yaml 의
원 단위 고정 한도(건당/종목당/일일 1,000만원)가 그 주문을 KIS 호출 전에
FAILED 시켰다.

결정 (사용자, 2026-09-29):
  * 원 단위 **고정** 상한(config.yaml max_order_amount / max_position_amount_
    per_symbol / max_daily_order_amount / 하위호환 키, env REAL_MAX_* 등)은
    더 이상 실계좌 주문을 막지 않는다. 예산을 1억원으로 올려도 1억 x1.5 가
    그대로 나가야 한다.
  * 주문금액의 상한은 전략층(base budget x SMART/slot 배수, 일일 노출 3.0,
    남은 일예산)과 KIS 실제 매수가능금액이 정한다.
  * 사용자가 UI 에서 '최대 주문금액' / '일일 주문금액' 을 **명시적으로**
    설정했을 때만 그 값을 추가 상한으로 쓴다. 넘으면 FAILED 가 아니라 한도
    안으로 수량을 줄인다(order_executor.apply_safety_cap).

저장 위치는 ``STATE_DIR`` (Render 에서는 persistent disk) 의 JSON 한 개다.
브로커가 매 주문마다 다시 읽으므로 저장 즉시 반영되고 재시작이 필요 없다.
파일이 없거나 깨져 있으면 '사용자 한도 없음' 으로 본다.
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from app.utils.data_paths import STATE_DIR
from app.utils.time_utils import KST

OVERRIDE_FILENAME = "real_order_user_limits.json"

#: 사용자가 설정할 수 있는 한도 — 1회 주문금액 / 하루 누적 주문금액.
LIMIT_KEYS = ("per_order", "daily")

#: UI 입력 상한 (0 하나 더 붙는 입력 오류 방지용 sanity bound).
MAX_LIMIT_KRW = 10_000_000_000


def override_path() -> Path:
    return STATE_DIR / OVERRIDE_FILENAME


def load_user_limits(path: Optional[Path] = None) -> dict[str, Any]:
    """사용자 한도. 설정된 키만 담긴다 — 비어 있으면 '사용자 한도 없음'."""
    p = path or override_path()
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, Any] = {}
    for key in LIMIT_KEYS:
        value = raw.get(key)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value <= MAX_LIMIT_KRW:
            out[key] = float(value)
    if out:
        out["updated_at"] = raw.get("updated_at", "")
        out["updated_by"] = raw.get("updated_by", "")
    return out


def validate(per_order: Optional[float], daily: Optional[float]) -> Optional[str]:
    """저장 전 검증. None 은 '그 한도는 설정 안 함'. 문제 없으면 None."""
    for label, value in (("최대 주문금액", per_order), ("일일 주문금액", daily)):
        if value is None:
            continue
        if not isinstance(value, (int, float)) or value <= 0:
            return f"{label}은 0보다 커야 합니다."
        if value > MAX_LIMIT_KRW:
            return f"{label}이 {MAX_LIMIT_KRW:,.0f}원을 넘습니다 (입력 오류 방지 상한)."
    if per_order is not None and daily is not None and daily < per_order:
        return "일일 주문금액은 최대 주문금액 이상이어야 합니다."
    if per_order is None and daily is None:
        return "설정할 한도가 없습니다. 한도를 없애려면 '사용자 한도 해제'를 쓰세요."
    return None


def save_user_limits(*, per_order: Optional[float], daily: Optional[float],
                     changed_by: str = "ui", path: Optional[Path] = None) -> dict[str, Any]:
    err = validate(per_order, daily)
    if err:
        return {"ok": False, "message": err}
    p = path or override_path()
    payload: dict[str, Any] = {"updated_at": datetime.now(KST).isoformat(), "updated_by": changed_by}
    if per_order is not None:
        payload["per_order"] = float(per_order)
    if daily is not None:
        payload["daily"] = float(daily)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)
    return {"ok": True, "message": "saved", "limits": payload}


def clear_user_limits(path: Optional[Path] = None) -> bool:
    p = path or override_path()
    try:
        p.unlink()
        return True
    except FileNotFoundError:
        return False
