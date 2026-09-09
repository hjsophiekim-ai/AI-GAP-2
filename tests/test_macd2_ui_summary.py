"""app.ui.macd2_summary 순수 단위 테스트 (Streamlit/AppTest 없음).

2026-09-09 사용자 요청: TP1 50% 부분익절만 된 상태는 왕복 0.5회(진행중),
잔량까지 청산되면 1.0회. 예전 화면은 SELL 레그 수를 그대로 세서 신규진입 1회를
"2건"으로 보여줬다.

AppTest 로 만들지 않은 이유: tests/macd2 전체 실행에는 아직 해결되지 않은
st.form 누수(테스트 전용)가 있어 페이지 렌더 횟수를 늘리면 무관한 테스트가
깨진다. 여기 규칙은 순수 함수라 Streamlit 없이 그대로 검증된다. 화면 렌더
자체(라벨/"진입 슬롯" 표시)는 tests/macd2/test_ui_page.py 의 기존 AppTest 가
계속 덮는다.
"""
from __future__ import annotations

from app.ui import macd2_summary as S


def _sell(position_after) -> dict:
    return {"side": "SELL", "position_after": position_after}


def test_no_sells_is_zero():
    assert S.count_round_trips([], has_open_position=False) == 0.0
    assert S.count_round_trips([], has_open_position=True) == 0.0


def test_single_full_close_is_one_round_trip():
    assert S.count_round_trips([_sell(0)], has_open_position=False) == 1.0


def test_tp1_partial_while_still_held_is_half_in_progress():
    """진입 1회 + TP1 절반청산, 잔량 보유 중 -> 0.5회 (진행중)."""
    rows = [_sell(508)]
    assert S.count_round_trips(rows, has_open_position=True) == 0.5
    assert S.has_in_progress_round_trip(rows, has_open_position=True) is True
    assert S.format_round_trips(0.5, in_progress=True) == "0.5회 (진행중)"


def test_tp1_partial_then_remainder_liquidated_is_exactly_one():
    """TP1 절반청산 + 잔량청산 = SELL 레그 2개지만 왕복은 1.0회.

    이것이 사용자가 "2건"으로 보던 바로 그 케이스다. 0.5 가 더해져 1.5 가
    되어서도 안 된다 -- 잔량까지 청산됐으면 보유 중이 아니다.
    """
    rows = [_sell(508), _sell(0)]
    assert S.count_round_trips(rows, has_open_position=False) == 1.0
    assert S.has_in_progress_round_trip(rows, has_open_position=False) is False
    assert S.format_round_trips(1.0, in_progress=False) == "1.0회"


def test_completed_round_trip_plus_a_second_position_still_partial():
    """완결 1건 + 다른 포지션이 부분익절 후 보유 중 -> 1.5회 (진행중)."""
    rows = [_sell(0), _sell(508)]
    assert S.count_round_trips(rows, has_open_position=True) == 1.5


def test_two_full_closes_are_two_round_trips():
    assert S.count_round_trips([_sell(0), _sell(0)], has_open_position=False) == 2.0


def test_blank_position_after_counts_as_a_full_close():
    """BROKER_DIRECT 수동청산은 position_before/after 가 빈 값이다 (원장 실측).
    예전 집계와 동일하게 완결로 세어 화면에서 사라지지 않게 한다."""
    assert S.position_after(_sell("")) is None
    assert S.count_round_trips([_sell("")], has_open_position=False) == 1.0
    assert S.is_position_closing_leg(_sell("")) is True
    assert S.is_partial_exit_leg(_sell("")) is False


def test_unparseable_position_after_is_treated_as_full_close():
    assert S.position_after(_sell("n/a")) is None
    assert S.count_round_trips([_sell("n/a")], has_open_position=False) == 1.0


def test_position_after_parses_float_strings():
    assert S.position_after(_sell("508.0")) == 508
    assert S.position_after(_sell(508)) == 508
    assert S.position_after(_sell("0")) == 0


def test_partial_leg_ignored_when_flat_even_if_remainder_leg_missing():
    """부분익절 레그만 있는데 이미 flat 이면(잔량청산이 원장에 아직 안 들어온
    드문 경합 상황) 0.5 를 붙이지 않는다 -- 보유 중일 때만 '진행중'이다."""
    rows = [_sell(508)]
    assert S.count_round_trips(rows, has_open_position=False) == 0.0
    assert S.has_in_progress_round_trip(rows, has_open_position=False) is False
