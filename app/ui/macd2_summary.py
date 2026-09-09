"""MACD2 "오늘의 거래 요약" 표시용 순수 집계 — Streamlit/원장/state 접근 없음.

2026-09-09 사용자 요청. 화면의 "왕복거래"가 매도 레그 개수(len(sell_rows))로
집계돼 있어서, TP1 50% 부분익절이 있으면 **신규진입 1회가 2건으로** 보였다
(부분익절 레그 + 잔량청산 레그 = SELL 2행).

여기 있는 것은 **표시 계산뿐**이다:

* 거래 로직/신호 로직/주문 로직과 무관하다.
* execution ledger / signal ledger 를 쓰지 않고 읽지도 않는다 -- 호출부가 이미
  읽어 둔 dict 리스트를 받는다.
* 손익 집계(total_gross_pnl/total_net_pnl/total_cost)는 건드리지 않는다. 그 값들은
  레그별 net_pnl 합이고 각 레그가 자기 비용을 이미 반영하므로 부분익절이 있어도
  이중계상이 없다 -- 틀린 것은 **횟수 하나뿐**이었다.
* 하루 3-slot 제한과도 무관하다. 캡은 time_window_3slot.resolve_slot 이
  state.tw2_3slot_slots_used_today 만 보고 독립적으로 판정하며, 그 카운터가
  증가하는 곳은 worker.py 의 진입 경로 두 곳뿐이다(매도/청산 경로에는 없다).
  따라서 TP1 부분익절도 잔량청산도 슬롯을 소비하지 않는다.

모듈로 뺀 이유: 페이지 파일(11_MACD_자동매매2.py)은 Streamlit 스크립트라
import 할 수 없어 순수 단위 테스트가 불가능하다. 이 규칙을 AppTest 로만
검증하려면 페이지 렌더를 한 번 더 돌려야 하는데, tests/macd2 전체 실행에는
아직 해결되지 않은 st.form 누수(테스트 전용)가 있어 AppTest 실행을 늘리면
무관한 테스트가 깨진다.
"""
from __future__ import annotations

from typing import Any, Iterable, Optional

#: 부분익절만 된(잔량 보유 중) 포지션이 기여하는 왕복 횟수.
IN_PROGRESS_ROUND_TRIP = 0.5


def position_after(row: dict[str, Any]) -> Optional[int]:
    """청산 후 잔량. 값이 없거나 숫자가 아니면 ``None``.

    ``BROKER_DIRECT`` 처럼 position_before/after 컬럼이 빈 레그가 실제로 존재하므로
    (원장 실측) 반드시 None 을 구분해야 한다.
    """
    raw = str(row.get("position_after") or "").strip()
    if not raw:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def is_position_closing_leg(row: dict[str, Any]) -> bool:
    """이 매도 레그가 포지션을 **완전히** 닫았는가.

    잔량 컬럼이 비어 있는 레그는 예전 집계와 동일하게 완결로 센다 -- 그렇게 해야
    BROKER_DIRECT 수동청산이 화면에서 사라지지 않는다.
    """
    remaining = position_after(row)
    return remaining is None or remaining <= 0


def is_partial_exit_leg(row: dict[str, Any]) -> bool:
    """TP1 절반청산처럼 잔량을 남긴 매도 레그인가."""
    remaining = position_after(row)
    return remaining is not None and remaining > 0


def count_round_trips(
    sell_rows: Iterable[dict[str, Any]],
    *,
    has_open_position: bool,
) -> float:
    """포지션 단위 왕복 횟수.

    * 완전청산 레그(position_after == 0) 1개 = 왕복 1.0회
    * 부분익절 레그가 있고 **아직 보유 중** = 진행중 0.5회
    * 부분익절 후 잔량까지 청산되면 그 포지션은 완전청산 레그로 세어져 1.0회가
      된다 -- 0.5 가 추가로 더해지지 않는다(보유 중이 아니므로).

    >>> count_round_trips([], has_open_position=False)
    0.0
    >>> rows = [{"position_after": "508"}]              # TP1 절반청산, 잔량 보유
    >>> count_round_trips(rows, has_open_position=True)
    0.5
    >>> rows = [{"position_after": "508"}, {"position_after": "0"}]
    >>> count_round_trips(rows, has_open_position=False)  # 잔량까지 청산
    1.0
    """
    rows = list(sell_rows)
    completed = sum(1 for row in rows if is_position_closing_leg(row))
    in_progress = (
        IN_PROGRESS_ROUND_TRIP
        if has_open_position and any(is_partial_exit_leg(row) for row in rows)
        else 0.0
    )
    return float(completed) + in_progress


def format_round_trips(count: float, *, in_progress: bool) -> str:
    """"1.0회" / "0.5회 (진행중)"."""
    return f"{count:.1f}회" + (" (진행중)" if in_progress else "")


def has_in_progress_round_trip(
    sell_rows: Iterable[dict[str, Any]],
    *,
    has_open_position: bool,
) -> bool:
    return bool(has_open_position) and any(is_partial_exit_leg(row) for row in sell_rows)
