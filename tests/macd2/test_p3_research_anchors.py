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
  · P3 적용  : 09:19 rescue(익절 + 잔량 승격) ->
               11:39 SMALL_WHIPSAW_HOLD_EXIT
               R0(50% 익절) net **+3.663** / Q2(20% 익절) net **+4.951**
               (research_20260928f_p3_chop_rescue/out/q_stats.txt)

2026-09-28 / 2026-09-03 H30 앵커는 이 파일 아래쪽 별도 절에 있다.
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
A0909_P3_NET = 4.951          # Q2 rescue(20% 익절) 후 runner 가 간 순수익
A0909_P3_NET_R0 = 3.663       # 참고: 이전 50% 익절이었을 때의 값


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


def test_0909_p3_rescue_takes_a_slice_and_promotes_the_rest():
    """실측: 09:19 에 부분익절 + 잔량 승격. Q2 이후 그 비중은 20% 다."""
    decision = p3_stack.evaluate(
        net_return_pct=A0909_B3_NET,
        entry_at=A0909_ENTRY, now=A0909_FIRST_TP,
        direction=Direction.UP_RED,
        already_rescued=False, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_PARTIAL_PROMOTE
    assert decision.exit_reason == config.EXIT_P3_PARTIAL
    assert decision.sell_fraction == pytest.approx(
        float(config.X2LITE_MORNING_TP1_SELL_RATIO))
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
    # Q2 (2026-09-28): 50% -> 20%, 그리고 그 20% 는 새 상수가 아니라
    # X2-lite 오전 TP1 익절비중을 그대로 재사용한 값이다.
    assert float(config.P3_RESCUE_SELL_RATIO) == 0.20
    assert (float(config.P3_RESCUE_SELL_RATIO)
            == float(config.X2LITE_MORNING_TP1_SELL_RATIO))
    # H30 (2026-09-28): 연장 hard deadline.
    assert float(config.P3_H30_EXT_MAX_HOLD_MIN) == 30.0
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


# ══ H30 앵커 (2026-09-28 연구 research_20260928f_p3_chop_rescue) ══════════
#
# 실측 출처
# ---------
#   out/h_stats.txt  표 2 (연장 거래 전량) / 표 4 (09/28 OOS)
#   out/oos_0928.txt (09/28 전략별 거래)
#   out/log_Q2.txt   80영업일 복리 523.0481
#   out/log_H30.txt  80영업일 복리 524.5533
#
# 80영업일 전체에서 연장이 발동한 거래는 **09/03 한 건뿐**이고, 09/28 은 그
# 바깥(82일 런)의 OOS 한 건이다. 두 건이 H30 의 전 표본이므로 둘 다 잠근다.

A0928_ENTRY = datetime(2026, 9, 28, 9, 9, tzinfo=KST)
A0928_MAXHOLD = datetime(2026, 9, 28, 9, 29, tzinfo=KST)    # 진입 + 20분
A0928_DEADLINE = datetime(2026, 9, 28, 9, 39, tzinfo=KST)   # 진입 + 30분
A0928_NET_AT_MAXHOLD = -0.2665     # h_stats 표 4 "20분net -0.2665"
A0928_Q2_NET = -0.267              # Q2: 09:29 GX_MAXHOLD 로 잘린 값
A0928_H30_NET = 3.262              # H30: 10:30 SMALL_WHIPSAW_HOLD_EXIT (81분)

A0903_ENTRY = datetime(2026, 9, 3, 11, 0, tzinfo=KST)
A0903_H50_AT = datetime(2026, 9, 3, 11, 18, tzinfo=KST)     # H50 HOLD 시작
A0903_MAXHOLD = datetime(2026, 9, 3, 11, 20, tzinfo=KST)    # 진입 + 20분
A0903_DEADLINE = datetime(2026, 9, 3, 11, 30, tzinfo=KST)   # 진입 + 30분
A0903_NET_AT_MAXHOLD = -0.821      # R0 / Q2 가 여기서 잘린 값
A0903_H30_NET = -0.621             # H30: 30분 GX_MAXHOLD
A0903_BASE_NET = -1.352            # 참고: B3 자체가 없었을 때


# ── L. 2026-09-28 09:09 인버스 (OOS) ─────────────────────────────────────
def test_0928_maxhold_and_deadline_land_on_the_research_clock():
    assert (A0928_MAXHOLD - A0928_ENTRY) == timedelta(
        minutes=float(config.P3_B3_MAX_HOLD_MIN))
    assert (A0928_DEADLINE - A0928_ENTRY) == timedelta(
        minutes=float(config.P3_H30_EXT_MAX_HOLD_MIN))


def test_0928_q2_cuts_the_trade_at_the_twenty_minute_mark():
    """Q2(= H30 이전)는 09:29 에 net -0.267 로 잘랐다. H50 은 보지 않는다."""
    decision = p3_stack.evaluate(
        net_return_pct=A0928_NET_AT_MAXHOLD,
        entry_at=A0928_ENTRY, now=A0928_MAXHOLD,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        h50_active=False, h30_active=False,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_MAXHOLD
    assert decision.reason == "Y3_FAIL_NET", "20분 시점 net 이 음수였다"
    assert decision.net_pct == pytest.approx(A0928_Q2_NET, abs=1e-3)


def test_0928_h30_extends_because_h50_was_already_holding():
    """실측: 이 거래는 h50=True 였다(BASE 가 10:30 SMALL_WHIPSAW_HOLD_EXIT 로
    끝난 것이 그 증거다). 그래서 H30 은 09:29 에 자르지 않고 09:39 까지 미룬다."""
    decision = p3_stack.evaluate(
        net_return_pct=A0928_NET_AT_MAXHOLD,
        entry_at=A0928_ENTRY, now=A0928_MAXHOLD,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        h50_active=True, h30_active=False,
    )
    assert decision.action == p3_stack.ACTION_EXTEND
    assert decision.exit_reason is None, "09:29 에 주문이 나가면 재현이 깨진다"


def test_0928_extension_does_not_partial_sell_because_one_percent_never_came():
    """실측 "+1%→runner False" -- 연장 구간에서 +1% 에 닿지 않았다.

    그래서 09/28 은 부분매도 없이 전량이 runner 로 넘어갔고 최종 +3.262 가 됐다
    (H40/H60 은 더 긴 창에서 +1% 에 닿아 20% 를 팔았고 그만큼 낮은 +2.839).
    """
    for elapsed in (21.0, 25.0, 29.0):
        decision = p3_stack.evaluate(
            net_return_pct=0.4,                    # 연장 내내 +1% 미만
            entry_at=A0928_ENTRY,
            now=A0928_ENTRY + timedelta(minutes=elapsed),
            direction=Direction.DOWN_BLUE,
            already_rescued=False, already_promoted=False,
            h50_active=True, h30_active=True,
        )
        assert decision.action == p3_stack.ACTION_EXTEND
        assert decision.sell_fraction == 0.0


def test_0928_deadline_promotes_the_runner(monkeypatch):
    """09:39 재판정에서 net>0 ∧ Y3 두 조건이 참이라 승격했다.

    연구 번들은 30분 시점의 net 을 숫자로 출력하지 않는다 -- 출력하는 것은
    "연장 True / +1%→runner False / 최종 SMALL_WHIPSAW_HOLD_EXIT +3.262" 다.
    승격이 일어나려면 그 시점 net 이 양수여야 하므로, 여기서는 **부호만**
    연구결과에서 가져오고 판정 자체는 실제 코드가 하게 둔다.
    """
    monkeypatch.setattr(p3_stack, "macd_gap_expanding",
                        lambda bars, now, direction: (True, "anchor=0928"))
    decision = p3_stack.evaluate(
        net_return_pct=0.10,                       # net > 0 (부호만 연구값)
        entry_at=A0928_ENTRY, now=A0928_DEADLINE,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        bars_3m=object(), etf_prev_price=5_700.0, etf_current_price=5_705.0,
        h50_active=True, h30_active=True,
    )
    assert decision.action == p3_stack.ACTION_PROMOTE
    assert decision.exit_reason is None
    assert decision.elapsed_min == pytest.approx(
        float(config.P3_H30_EXT_MAX_HOLD_MIN))


def test_0928_promoted_runner_is_handed_back_and_reaches_the_h50_exit():
    """승격 이후 B3 는 개입하지 않는다 -- 그래서 10:30 H50 청산까지 갔다(+3.262).

    81분 경과 + max-hold 조건을 다시 물어도 HOLD 여야 한다.
    """
    decision = p3_stack.evaluate(
        net_return_pct=A0928_H30_NET,
        entry_at=A0928_ENTRY,
        now=datetime(2026, 9, 28, 10, 30, tzinfo=KST),
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=True,
        h50_active=True, h30_active=True,
    )
    assert decision.action == p3_stack.ACTION_HOLD


def test_0928_h30_beats_q2_on_this_trade():
    """방향 재현: 같은 거래에서 H30 이 Q2 보다 runner 를 살렸다."""
    assert A0928_H30_NET > A0928_Q2_NET
    assert A0928_H30_NET == pytest.approx(3.262)
    assert A0928_H30_NET - A0928_Q2_NET == pytest.approx(3.529, abs=1e-3)


# ── M. 2026-09-03 11:00 (80일 안의 유일한 연장 거래) ─────────────────────
def test_0903_h50_started_before_the_max_hold_mark():
    """11:18 에 H50 이 켜졌고 max-hold 는 11:20 이다 -- 그래서 연장 대상이다."""
    assert A0903_H50_AT < A0903_MAXHOLD
    assert (A0903_MAXHOLD - A0903_ENTRY) == timedelta(
        minutes=float(config.P3_B3_MAX_HOLD_MIN))


def test_0903_q2_would_have_cut_it_at_minus_zero_eight_two():
    decision = p3_stack.evaluate(
        net_return_pct=A0903_NET_AT_MAXHOLD,
        entry_at=A0903_ENTRY, now=A0903_MAXHOLD,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        h50_active=False, h30_active=False,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_MAXHOLD
    assert decision.net_pct == pytest.approx(A0903_NET_AT_MAXHOLD)


def test_0903_h30_extends_at_the_max_hold_mark():
    decision = p3_stack.evaluate(
        net_return_pct=A0903_NET_AT_MAXHOLD,
        entry_at=A0903_ENTRY, now=A0903_MAXHOLD,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        h50_active=True, h30_active=False,
    )
    assert decision.action == p3_stack.ACTION_EXTEND


def test_0903_deadline_exits_instead_of_promoting_forever():
    """**핵심 계약**: H30 은 유예 후 재판정이지 무기한 승격이 아니다.

    11:30 에 net 은 여전히 음수(-0.621)였으므로 Y3 는 net<=0 에서 바로 실패하고
    전량청산된다. 여기서 승격이 나오면 N1 runner 로 무기한 넘어가 버린다.
    """
    decision = p3_stack.evaluate(
        net_return_pct=A0903_H30_NET,
        entry_at=A0903_ENTRY, now=A0903_DEADLINE,
        direction=Direction.DOWN_BLUE,
        already_rescued=False, already_promoted=False,
        # H50 은 이 시점에도 켜져 있었다 -- 그래도 30분 deadline 이 이긴다.
        h50_active=True, h30_active=True,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.promote is False
    assert decision.reason == "Y3_FAIL_NET"
    assert decision.exit_reason == config.EXIT_P3_H30_MAXHOLD
    assert decision.sell_fraction == pytest.approx(1.0)
    assert decision.elapsed_min == pytest.approx(
        float(config.P3_H30_EXT_MAX_HOLD_MIN))


def test_0903_h30_reduced_the_loss_relative_to_q2():
    """방향 재현: 손실이 -0.821 -> -0.621 로 **조금** 줄었다."""
    assert A0903_H30_NET > A0903_NET_AT_MAXHOLD
    assert A0903_H30_NET - A0903_NET_AT_MAXHOLD == pytest.approx(0.200, abs=1e-3)
    assert A0903_H30_NET < 0.0, "이익으로 뒤집힌 것이 아니라 손실이 줄었을 뿐이다"
    assert A0903_H30_NET > A0903_BASE_NET


# ── 80영업일 replay 연구값 (재계산이 아니라 **기록**이다) ────────────────
#: 봉단위 replay 엔진은 이 저장소에 없다(연구 랩 전용). 그래서 여기서는 숫자를
#: 다시 계산하지 않고, 채택근거가 된 연구 출력값과 그 부호/순서를 고정한다.
#: 이 값이 바뀌면 채택근거 자체가 바뀐 것이므로 재검증이 필요하다.
REPLAY_80D = {"R0": 513.289, "Q2": 523.048, "H30": 524.553,
              "H40": 525.556, "H60": 524.052}


def test_eighty_day_replay_direction_matches_the_research():
    assert REPLAY_80D["Q2"] == pytest.approx(523.05, abs=5e-3)
    assert REPLAY_80D["H30"] == pytest.approx(524.55, abs=5e-3)
    # Q2 는 R0(50% 익절) 보다 낫고, H30 은 Q2 보다 낫다 -- 채택의 두 근거다.
    assert REPLAY_80D["R0"] < REPLAY_80D["Q2"] < REPLAY_80D["H30"]
    assert REPLAY_80D["H30"] - REPLAY_80D["Q2"] == pytest.approx(1.505, abs=1e-3)
    assert REPLAY_80D["Q2"] - REPLAY_80D["R0"] == pytest.approx(9.759, abs=1e-3)
