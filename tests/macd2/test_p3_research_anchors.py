"""P3 연구 앵커 회귀 — 2026-09-22 Y3 / 2026-09-09 P3 (사용자 요구 §16).

두 앵커는 80영업일 replay(연구번들 ``research_20260927_p3_final_adoption``,
lab80/d2.pkl)에서 나온 **실측값**이다. 봉단위 replay 엔진은 저장소에 없으므로
(연구 랩에만 있다) 여기서는 그 실측 입력값을 그대로 넣어 **판정 함수의 계약**을
잠근다 -- 즉 "같은 입력이 들어오면 같은 결정이 나오는가" 를 회귀로 고정한다.

실측 앵커
---------
2026-09-22 12:15 DOWN_BLUE (CHOP 진입)
  · B3 단독  : 12:35 GX_MAXHOLD, net **+0.302**
  · Y3 적용  : 12:35 **승격**(gx_grace/gx_prom 둘 다 12:35) ->
               13:19 TIME_WINDOW_AFTERNOON_TP, net **+4.026**
  즉 Y3 는 max-hold 시점에 net>0 ∧ gap 확대 ∧ ETF 추종이 **전부 참**이었다.

2026-09-09 09:15 UP_RED (CHOP 진입)
  · B3 단독  : 09:19 GX_TP, net **+1.515**  (진입 4분 만에 +1%)
  · P3 적용  : 09:19 rescue(50% 익절 + 잔량 승격) ->
               11:39 SMALL_WHIPSAW_HOLD_EXIT, net **+3.663** (peak 5.637)
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, p3_stack
from app.trading.macd2.models import Direction

KST = config.KST

# ── 실측 앵커 (lab80/d2.pkl) ──────────────────────────────────────────────
A0922_ENTRY = datetime(2026, 9, 22, 12, 15, tzinfo=KST)
A0922_MAXHOLD = datetime(2026, 9, 22, 12, 35, tzinfo=KST)
A0922_B3_NET = 0.302          # B3 단독일 때 max-hold 에서 잘린 순수익
A0922_Y3_NET = 4.026          # Y3 승격 후 13:19 오후TP 까지 간 순수익

A0909_ENTRY = datetime(2026, 9, 9, 9, 15, tzinfo=KST)
A0909_FIRST_TP = datetime(2026, 9, 9, 9, 19, tzinfo=KST)
A0909_B3_NET = 1.515          # B3 단독일 때 +1% 에서 전량 익절
A0909_P3_NET = 3.663          # P3 rescue 후 runner 가 간 순수익


# ── 2026-09-22 : Y3 ──────────────────────────────────────────────────────
def test_0922_maxhold_is_exactly_twenty_minutes_after_entry():
    assert (A0922_MAXHOLD - A0922_ENTRY) == timedelta(
        minutes=float(config.P3_B3_MAX_HOLD_MIN))


def test_0922_b3_alone_cuts_the_runner_at_max_hold():
    """Y3 조건 중 하나라도 거짓이면 +0.302 에서 잘린다 -- 이것이 B3 단독 결과다."""
    decision = p3_stack.evaluate(
        net_return_pct=A0922_B3_NET,
        entry_at=A0922_ENTRY, now=A0922_MAXHOLD,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        bars_3m=None,                      # gap 판정 불가 -> 조건 거짓
        etf_prev_price=100.0, etf_current_price=101.0,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_MAXHOLD
    assert decision.reason == "Y3_FAIL_COND"
    assert decision.net_pct == pytest.approx(A0922_B3_NET)
    assert decision.elapsed_min == pytest.approx(float(config.P3_B3_MAX_HOLD_MIN))


def test_0922_y3_promotes_when_all_three_conditions_hold(monkeypatch):
    """실측: 12:35 에 net>0 ∧ gap 확대 ∧ ETF 추종이 전부 참 -> 승격.

    gap 조건은 봉 데이터에 의존하므로, 그 판정만 실측 결과(True)로 고정하고
    나머지(net>0 / ETF 추종 / max-hold 도달)는 실제 코드가 계산하게 둔다.
    """
    monkeypatch.setattr(p3_stack, "macd_gap_expanding",
                        lambda bars, now, direction: (True, "anchor=0922"))
    decision = p3_stack.evaluate(
        net_return_pct=A0922_B3_NET,
        entry_at=A0922_ENTRY, now=A0922_MAXHOLD,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        bars_3m=object(),
        etf_prev_price=100.0, etf_current_price=100.0,   # >= 이면 추종
    )
    assert decision.action == p3_stack.ACTION_PROMOTE
    assert decision.promote is True
    assert decision.exit_reason is None, "승격은 청산하지 않는다"
    assert "gap" in decision.conditions and "etf" in decision.conditions


def test_0922_promotion_hands_the_runner_back_to_the_base_ladder(monkeypatch):
    """승격 이후에는 B3 가 더 이상 개입하지 않는다 -- 그래서 13:19 오후TP 까지
    갈 수 있었다(+4.026). 승격된 포지션에 B3 를 다시 물으면 HOLD 여야 한다."""
    decision = p3_stack.evaluate(
        net_return_pct=A0922_Y3_NET,
        entry_at=A0922_ENTRY,
        now=datetime(2026, 9, 22, 13, 19, tzinfo=KST),
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=True,
        bars_3m=None,
    )
    assert decision.action == p3_stack.ACTION_HOLD
    assert decision.exit_reason is None


def test_0922_would_have_exited_if_net_were_not_positive():
    """net<=0 이면 gap/ETF 를 보지도 않고 즉시 청산이다(연구엔진과 동일 순서)."""
    decision = p3_stack.evaluate(
        net_return_pct=-0.01,
        entry_at=A0922_ENTRY, now=A0922_MAXHOLD,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        bars_3m=object(), etf_prev_price=100.0, etf_current_price=101.0,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.reason == "Y3_FAIL_NET"


# ── 2026-09-09 : P3 ──────────────────────────────────────────────────────
def test_0909_first_one_percent_arrives_within_the_rescue_window():
    elapsed = (A0909_FIRST_TP - A0909_ENTRY).total_seconds() / 60.0
    assert elapsed == pytest.approx(4.0)
    assert elapsed <= float(config.P3_RESCUE_MAX_MIN)


def test_0909_p3_rescue_takes_half_and_promotes_the_rest():
    """실측: 09:19 에 50% 익절 + 잔량 승격."""
    decision = p3_stack.evaluate(
        net_return_pct=A0909_B3_NET,
        entry_at=A0909_ENTRY, now=A0909_FIRST_TP,
        direction=Direction.UP_RED,
        already_rescued=False, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_PARTIAL_PROMOTE
    assert decision.exit_reason == config.EXIT_P3_PARTIAL
    assert decision.sell_fraction == pytest.approx(0.5)
    assert decision.rescue is True and decision.promote is True
    assert decision.elapsed_min == pytest.approx(4.0)


def test_0909_b3_alone_would_have_taken_the_whole_position_at_one_percent():
    """rescue 가 없었다면 같은 시점에 전량 익절(+1.515)로 끝났다."""
    decision = p3_stack.evaluate(
        net_return_pct=A0909_B3_NET,
        entry_at=A0909_ENTRY, now=A0909_FIRST_TP,
        direction=Direction.UP_RED,
        already_rescued=True,          # rescue 를 이미 썼다고 두면 B3 경로
        already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_TP
    assert decision.sell_fraction == pytest.approx(1.0)


def test_0909_runner_is_no_longer_governed_by_b3_after_the_rescue():
    """승격된 잔량은 기존 N1/C1 래더로 돌아가 11:39 까지 간다(+3.663)."""
    decision = p3_stack.evaluate(
        net_return_pct=A0909_P3_NET,
        entry_at=A0909_ENTRY,
        now=datetime(2026, 9, 9, 11, 39, tzinfo=KST),
        direction=Direction.UP_RED,
        already_rescued=True, already_promoted=True,
    )
    assert decision.action == p3_stack.ACTION_HOLD


# ── 임계값이 연구값에서 움직이지 않았는지 ────────────────────────────────
def test_research_thresholds_are_unchanged():
    """연구에서 확정된 값이다. 바뀌면 두 앵커가 재현되지 않는다."""
    assert float(config.P3_B3_TP_PCT) == 1.0
    assert float(config.P3_B3_SL_PCT) == 1.0
    assert float(config.P3_B3_MAX_HOLD_MIN) == 20.0
    assert float(config.P3_RESCUE_MAX_MIN) == 6.0
    assert float(config.P3_RESCUE_SELL_RATIO) == 0.5
    assert int(config.P3_DETECTOR_WINDOW) == 10
    assert float(config.P3_H50_RATE_MIN) == 0.40
    assert float(config.P3_TP1_RATE_MAX) == 0.20


# ── 저장소에 커밋된 seed fixture ─────────────────────────────────────────
def test_committed_seed_fixture_reproduces_the_research_regime():
    """배포용 seed 가 연구 판정을 그대로 재현하는지 -- 내일 장 시작 시점의
    regime 이 이 파일 하나로 결정되므로 여기서 잠근다."""
    import json
    from pathlib import Path

    from app.trading.macd2 import chop_regime

    path = (Path(__file__).parent.parent.parent / "data" / "validation" / "macd2"
            / "p3_regime_stack_20260927" / "shadow_seed.json")
    blob = json.loads(path.read_text(encoding="utf-8"))
    rows = [chop_regime.ShadowTrade.from_dict(r) for r in blob["trades"]]
    rows = [r for r in rows if r is not None]

    assert len(rows) == 30
    # 미래정보 금지 -- 2026-09-22 이후 거래가 한 건도 없어야 한다.
    assert max(r.trading_date for r in rows) == "20260922"

    decision = chop_regime.evaluate(rows)
    assert decision.regime == chop_regime.REGIME_CHOP
    assert decision.h50_rate == pytest.approx(0.50)
    assert decision.tp1_rate == pytest.approx(0.00)
    # 마지막 거래는 9/22 Y3 앵커 그 자체다.
    assert rows[-1].exit_time.startswith("2026-09-22T13:19")
    assert rows[-1].net_pct == pytest.approx(A0922_Y3_NET, abs=1e-3)
