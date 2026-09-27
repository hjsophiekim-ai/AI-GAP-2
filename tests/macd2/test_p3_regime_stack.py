"""P3 regime stack — detector / B3 / P3 / Y3 / 영속성 / 전략모드 (2026-09-27).

사용자 요구 §15 의 A~K 를 그대로 옮긴 테스트다. 실제 주문경로(worker tick)를
타는 것은 별도 파일(test_p3_worker.py)이고, 여기서는 판정·상태·영속성을 잠근다.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import (
    chop_regime,
    config,
    p3_stack,
    state_store,
    strategy_mode,
)
from app.trading.macd2.models import Direction

KST = config.KST


def _at(hh: int, mm: int, *, day: int = 22) -> datetime:
    return datetime(2026, 9, day, hh, mm, tzinfo=KST)


def _trade(i: int, *, h50: bool, tp1: bool, day: int = 22, minute: int = 0) -> chop_regime.ShadowTrade:
    """완료 shadow 거래 하나. exit_time 순서가 detector 의 정렬 기준이다."""
    entry = _at(9, 0, day=day) + timedelta(minutes=minute + i)
    exit_ = entry + timedelta(minutes=30)
    return chop_regime.ShadowTrade(
        shadow_trade_id=chop_regime.make_shadow_trade_id(entry, "UP_RED", 1),
        trading_date=entry.strftime("%Y%m%d"),
        entry_time=entry.isoformat(),
        exit_time=exit_.isoformat(),
        direction="UP_RED",
        slot=1,
        entry_price=10000.0,
        exit_price=10100.0,
        net_pct=1.0,
        h50_intervened=h50,
        tp1_hit=tp1,
    )


def _series(h50_count: int, tp1_count: int, n: int = 10) -> list[chop_regime.ShadowTrade]:
    return [_trade(i, h50=(i < h50_count), tp1=(i < tp1_count)) for i in range(n)]


# ── D. CHOP detector 경계 ────────────────────────────────────────────────
@pytest.mark.parametrize(
    "h50, tp1, expected",
    [
        (4, 2, chop_regime.REGIME_CHOP),    # 0.40 / 0.20 -- 경계 포함
        (3, 2, chop_regime.REGIME_TREND),   # H50 0.30 < 0.40
        (4, 3, chop_regime.REGIME_TREND),   # TP1 0.30 > 0.20
        (10, 0, chop_regime.REGIME_CHOP),
        (0, 10, chop_regime.REGIME_TREND),
    ],
)
def test_slow_detector_boundaries(h50, tp1, expected):
    decision = chop_regime.evaluate(_series(h50, tp1))
    assert decision.regime == expected
    assert decision.h50_rate == pytest.approx(h50 / 10.0)
    assert decision.tp1_rate == pytest.approx(tp1 / 10.0)


# ── C. WARMUP ───────────────────────────────────────────────────────────
def test_fewer_than_ten_completed_trades_is_warmup():
    decision = chop_regime.evaluate(_series(9, 0, n=9))
    assert decision.regime == chop_regime.REGIME_WARMUP
    assert decision.is_warmup
    assert decision.sample == 9
    assert decision.h50_rate is None


def test_detector_uses_only_the_last_ten_completed_trades():
    """11건이면 **가장 오래된 1건은 빠진다** -- 최근 10건만 본다."""
    old_trend = [_trade(i, h50=False, tp1=True, minute=0) for i in range(5)]
    recent_chop = [_trade(i, h50=True, tp1=False, minute=100) for i in range(10)]
    decision = chop_regime.evaluate(old_trend + recent_chop)
    assert decision.regime == chop_regime.REGIME_CHOP
    assert decision.h50_rate == pytest.approx(1.0)


def test_as_of_excludes_trades_that_have_not_finished_yet():
    """판정시각보다 **늦게 끝난** 거래는 세지 않는다(미래참조 차단)."""
    trades = _series(10, 0)
    cutoff = trades[5].exit_time
    decision = chop_regime.evaluate(trades, as_of=cutoff)
    assert decision.sample == 5
    assert decision.regime == chop_regime.REGIME_WARMUP


# ── 안정키 / 중복방지 ────────────────────────────────────────────────────
def test_shadow_trade_id_is_deterministic():
    a = chop_regime.make_shadow_trade_id(_at(9, 6), Direction.UP_RED, 2)
    b = chop_regime.make_shadow_trade_id(_at(9, 6), Direction.UP_RED, 2)
    c = chop_regime.make_shadow_trade_id(_at(9, 9), Direction.UP_RED, 2)
    assert a == b
    assert a != c


# ── E. 재시작 복원 ───────────────────────────────────────────────────────
def test_ledger_roundtrip_restores_identical_rates_and_regime(tmp_path, monkeypatch):
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", tmp_path / "shadow.json")
    trades = _series(5, 1)
    chop_regime.save_ledger(trades, source="test")
    before = chop_regime.evaluate(trades)

    restored = chop_regime.load_ledger()
    after = chop_regime.evaluate(restored)

    assert len(restored) == len(trades)
    assert after.regime == before.regime == chop_regime.REGIME_CHOP
    assert after.h50_rate == before.h50_rate
    assert after.tp1_rate == before.tp1_rate
    assert [t.shadow_trade_id for t in restored] == [t.shadow_trade_id for t in trades]


def test_append_trade_rejects_duplicate_id(tmp_path, monkeypatch):
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", tmp_path / "shadow.json")
    t = _trade(0, h50=True, tp1=False)
    assert chop_regime.append_trade(t) is True
    assert chop_regime.append_trade(t) is False
    assert len(chop_regime.load_ledger()) == 1


def test_ledger_keeps_at_least_thirty_recent_trades(tmp_path, monkeypatch):
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", tmp_path / "shadow.json")
    chop_regime.save_ledger(_series(0, 0, n=60), source="test")
    assert len(chop_regime.load_ledger()) == max(
        int(config.P3_SHADOW_LEDGER_KEEP), int(config.P3_DETECTOR_WINDOW))


# ── fail-safe = BASE ─────────────────────────────────────────────────────
def test_corrupt_ledger_falls_back_to_warmup_not_an_exception(tmp_path, monkeypatch):
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    path = tmp_path / "shadow.json"
    path.write_text("{ this is not json", encoding="utf-8")
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", path)
    decision = chop_regime.current_regime()
    assert decision.regime == chop_regime.REGIME_WARMUP
    assert decision.sample == 0


def test_ledger_rows_without_stable_id_are_dropped(tmp_path, monkeypatch):
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    path = tmp_path / "shadow.json"
    payload = {"schema_version": 1, "trades": [
        {"shadow_trade_id": "", "exit_time": "2026-09-22T10:00:00+09:00"},
        {"exit_time": "2026-09-22T10:00:00+09:00"},
        _trade(0, h50=True, tp1=False).to_dict(),
    ]}
    path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", path)
    assert len(chop_regime.load_ledger()) == 1


# ── G. P3 TP RUNNER RESCUE ───────────────────────────────────────────────
def test_p3_rescue_fires_when_one_percent_reached_within_six_minutes():
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=1.0, entry_at=entry, now=entry + timedelta(minutes=4),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_PARTIAL_PROMOTE
    assert decision.sell_fraction == pytest.approx(0.5)
    assert decision.promote is True and decision.rescue is True
    assert decision.exit_reason == config.EXIT_P3_PARTIAL


def test_p3_rescue_does_not_fire_after_six_minutes_full_b3_tp_instead():
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=1.0, entry_at=entry, now=entry + timedelta(minutes=9),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_TP
    assert decision.sell_fraction == pytest.approx(1.0)


def test_six_minutes_exactly_is_still_a_rescue():
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=1.0, entry_at=entry, now=entry + timedelta(minutes=6),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_PARTIAL_PROMOTE


def test_rescue_never_repeats_for_the_same_position():
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=1.5, entry_at=entry, now=entry + timedelta(minutes=4),
        direction=Direction.UP_RED, already_rescued=True, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_TP


def test_promoted_position_is_left_entirely_alone():
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=5.0, entry_at=entry, now=entry + timedelta(minutes=45),
        direction=Direction.UP_RED, already_rescued=True, already_promoted=True,
    )
    assert decision.action == p3_stack.ACTION_HOLD


# ── B3 SL ────────────────────────────────────────────────────────────────
def test_b3_stop_loss_at_minus_one_percent():
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=-1.0, entry_at=entry, now=entry + timedelta(minutes=3),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_SL


def test_b3_holds_between_thresholds():
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=0.3, entry_at=entry, now=entry + timedelta(minutes=10),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_HOLD


def test_max_hold_is_not_evaluated_on_the_tick_stage():
    """이른 청산 체인에서는 max-hold 를 보지 않는다 -- 봉이 없어서 Y3 가
    항상 거짓이 되면 승격 기회를 통째로 잃기 때문이다."""
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=0.3, entry_at=entry, now=entry + timedelta(minutes=25),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        allow_max_hold=False,
    )
    assert decision.action == p3_stack.ACTION_HOLD


# ── H. Y3 MAX-HOLD PROMOTION ─────────────────────────────────────────────
def _bars(closes, *, start=_at(12, 0)):
    import pandas as pd

    return pd.DataFrame({
        "datetime": [start + timedelta(minutes=3 * i) for i in range(len(closes))],
        "close": [float(c) for c in closes],
        "open": [float(c) for c in closes],
        "high": [float(c) for c in closes],
        "low": [float(c) for c in closes],
        "volume": [1000] * len(closes),
    })


def test_y3_exits_immediately_when_net_is_not_positive():
    entry = _at(12, 15)
    decision = p3_stack.evaluate(
        net_return_pct=-0.1, entry_at=entry, now=entry + timedelta(minutes=20),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        bars_3m=_bars(range(100, 140)), etf_prev_price=100.0, etf_current_price=101.0,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.exit_reason == config.EXIT_B3_MAXHOLD
    assert decision.reason == "Y3_FAIL_NET"


def test_y3_exits_when_etf_is_not_following_even_if_gap_expands():
    entry = _at(12, 15)
    bars = _bars(range(100, 160))
    decision = p3_stack.evaluate(
        net_return_pct=0.3, entry_at=entry,
        now=bars["datetime"].iloc[-1] + timedelta(minutes=3),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        bars_3m=bars, etf_prev_price=100.0, etf_current_price=99.0,
    )
    assert decision.action == p3_stack.ACTION_EXIT
    assert decision.reason == "Y3_FAIL_COND"
    assert "etf" not in decision.conditions.split("|")[0]


def test_y3_promotes_when_net_positive_and_gap_expanding_and_etf_following():
    entry = _at(12, 15)
    # 가속 상승 -> MACD 히스토그램이 보유방향(UP)으로 확대된다.
    closes = [100 + i * i * 0.05 for i in range(60)]
    bars = _bars(closes)
    decision = p3_stack.evaluate(
        net_return_pct=0.3, entry_at=entry,
        now=bars["datetime"].iloc[-1] + timedelta(minutes=3),
        direction=Direction.UP_RED, already_rescued=False, already_promoted=False,
        bars_3m=bars, etf_prev_price=100.0, etf_current_price=101.0,
    )
    assert decision.action == p3_stack.ACTION_PROMOTE
    assert decision.promote is True
    assert "gap" in decision.conditions and "etf" in decision.conditions


def test_y3_gap_condition_is_direction_aware():
    """같은 봉이라도 인버스 보유면 gap 조건의 부호가 뒤집힌다."""
    bars = _bars([100 + i * i * 0.05 for i in range(60)])
    now = bars["datetime"].iloc[-1] + timedelta(minutes=3)
    up_ok, _ = p3_stack.macd_gap_expanding(bars, now, Direction.UP_RED)
    down_ok, _ = p3_stack.macd_gap_expanding(bars, now, Direction.DOWN_BLUE)
    assert up_ok is True
    assert down_ok is False


# ── I. 미래참조 차단 ─────────────────────────────────────────────────────
def test_forming_bar_is_never_used_for_the_gap_condition():
    """마지막 봉이 아직 **형성 중**이면 그 봉은 쓰지 않는다."""
    bars = _bars([100 + i * i * 0.05 for i in range(60)])
    last_start = bars["datetime"].iloc[-1]
    # 봉 시작 + 2분 -> 아직 완성 전이므로 직전 봉이 마지막 완성봉이어야 한다.
    idx = p3_stack.last_completed_bar_index(bars, last_start + timedelta(minutes=2))
    assert idx == len(bars) - 2
    # 봉 시작 + 3분 -> 이제 완성됐다.
    idx_done = p3_stack.last_completed_bar_index(bars, last_start + timedelta(minutes=3))
    assert idx_done == len(bars) - 1


def test_appending_a_forming_bar_does_not_change_the_decision():
    """미완성 봉을 하나 더 붙여도 판정이 바뀌지 않아야 한다."""
    closes = [100 + i * i * 0.05 for i in range(60)]
    bars = _bars(closes)
    now = bars["datetime"].iloc[-1] + timedelta(minutes=3)
    before, _ = p3_stack.macd_gap_expanding(bars, now, Direction.UP_RED)

    # 형성 중인(= now 기준 아직 완성되지 않은) 봉을 하나 붙인다. 종가는
    # 판정을 뒤집을 만큼 극단적으로 잡는다.
    bars_plus = _bars(closes + [0.01])
    after, _ = p3_stack.macd_gap_expanding(bars_plus, now, Direction.UP_RED)
    assert before == after


def test_no_completed_bar_means_condition_is_false_not_guessed():
    bars = _bars([100.0, 101.0])
    idx = p3_stack.last_completed_bar_index(bars, bars["datetime"].iloc[0])
    assert idx is None
    ok, tag = p3_stack.macd_gap_expanding(bars, bars["datetime"].iloc[0], Direction.UP_RED)
    assert ok is False and tag == "no_completed_bar"


# ── 포지션 스냅샷 / 중복 방지 (J, K) ─────────────────────────────────────
def _n1_state(*, p3: bool):
    state = state_store.default_state()
    strategy_mode.apply(state, strategy_mode.MODE_P3 if p3 else strategy_mode.MODE_N1)
    return state


def test_entry_regime_snapshot_is_not_retroactively_changed():
    state = _n1_state(p3=True)
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    assert state.p3_position_active is True
    assert p3_stack.governs_position(state) is True

    # 보유 중 detector 가 TREND 로 바뀌어도 이 포지션의 관리모드는 그대로다.
    state.p3_last_regime = chop_regime.REGIME_TREND
    assert p3_stack.governs_position(state) is True


def test_trend_entry_never_enters_the_p3_stack():
    state = _n1_state(p3=True)
    p3_stack.note_entry_regime(state, chop_regime.REGIME_TREND)
    assert state.p3_position_active is False
    assert p3_stack.governs_position(state) is False


def test_warmup_entry_never_enters_the_p3_stack():
    state = _n1_state(p3=True)
    p3_stack.note_entry_regime(state, chop_regime.REGIME_WARMUP)
    assert p3_stack.governs_position(state) is False


def test_first_tp_is_recorded_exactly_once():
    state = _n1_state(p3=True)
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    assert p3_stack.note_first_tp(state, _at(12, 19)) is True
    assert p3_stack.note_first_tp(state, _at(12, 22)) is False
    assert state.p3_first_tp_at == _at(12, 19).isoformat()


def test_promotion_hands_the_position_back_to_the_base_ladder():
    state = _n1_state(p3=True)
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    assert p3_stack.governs_position(state) is True
    p3_stack.note_y3_promoted(state, _at(12, 35))
    assert p3_stack.is_promoted(state) is True
    assert p3_stack.governs_position(state) is False
    assert p3_stack.position_mode(state) == p3_stack.MODE_Y3_RUNNER


def test_position_mode_labels():
    state = _n1_state(p3=True)
    assert p3_stack.position_mode(state) == p3_stack.MODE_BASE
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    assert p3_stack.position_mode(state) == p3_stack.MODE_B3
    p3_stack.note_rescued(state, _at(12, 19))
    assert p3_stack.position_mode(state) == p3_stack.MODE_P3_RUNNER


# ── A/B. P3 OFF / N1 모드에서는 스택이 아예 돌지 않는다 ──────────────────
def test_p3_stack_is_inert_in_n1_mode():
    state = _n1_state(p3=False)
    assert p3_stack.is_active(state) is False
    p3_stack.note_entry_regime(state, chop_regime.REGIME_CHOP)
    # regime 스냅샷이 CHOP 이어도 P3 가 꺼져 있으면 B3 가 주인이 되지 않는다.
    assert p3_stack.governs_position(state) is False


def test_p3_stack_requires_n1_family_mode():
    state = state_store.default_state()
    state.p3_enabled = True
    for flag in ("time_window_n1_filter_enabled", "time_window_h50_filter_enabled",
                 "time_window_x2lite_filter_enabled"):
        setattr(state, flag, False)
    state.time_window_h50_filter_enabled = True
    assert p3_stack.is_supported_mode(state) is False
    assert p3_stack.is_active(state) is False
