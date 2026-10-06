"""E 전략 (EARLY-UP-FAST + RS125) — 계약 고정 (2026-10-06).

잠그는 축:

  1~5   EARLY-PASS 판정 (즉시진입 / UP-FAST 는 UP 만 / c2·c3 폐기조건 불변 / trigger)
  6~9   돌파 대기 (만료 / 날짜 rollover / 돌파 판정 / 체결 signal_id 분리)
  10~14 RS125 (미래표본 차단 / 최소표본 / 당일봉 조건 / 일일한도 안 증액 / parity 표 우선)
  15~18 모드·상태 (E 는 P3 스택 위 / E->P3 대기 정리 / 재시작 복원 / rollover)
  19~22 worker 보조 (N1·P3 no-op / hard safety / RS 원자료 당일봉 집계)

거래단위 parity(85영업일 production replay)는 이 파일이 아니라 연구 하네스로
검증한다 -- research_20261004_chop_staged/REPORT_E_PARITY.md.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from app.trading.macd2 import config, e_strategy, state_store, strategy_mode, worker
from app.trading.macd2.models import Direction

KST = config.KST
T0 = datetime(2026, 9, 22, 9, 6, tzinfo=KST)


def _bars(rows):
    """(시각, open, high, low, close) -> 3분봉 프레임. 앞에 MACD 워밍업 봉을 깐다."""
    warm = []
    t = T0 - timedelta(minutes=3 * 60)
    px = 2_000_000.0
    for i in range(54):
        warm.append((t + timedelta(minutes=3 * i), px, px + 1000, px - 1000, px))
    data = warm + list(rows)
    return pd.DataFrame(data, columns=["datetime", "open", "high", "low", "close"])


FLAG = T0 - timedelta(minutes=6)
CONF = T0 - timedelta(minutes=3)


def _up_bars(*, confirm_body_up=True, widen=True):
    """플래그봉 고가 2,010,000 / 확정봉 고가 2,012,000 -> UP trigger 2,012,000."""
    fc = 2_004_000.0 if widen else 2_000_500.0
    cc = (2_010_000.0 if confirm_body_up else 2_003_000.0) if widen else 2_000_000.0
    co = 2_006_000.0
    return _bars([
        (FLAG, 2_000_000.0, 2_010_000.0, 1_999_000.0, fc),
        (CONF, co, 2_012_000.0, 2_001_000.0, cc),
    ])


def _early(bars, price, direction=Direction.UP_RED):
    return e_strategy.evaluate_early_pass(bars_3m=bars, flag_bar_dt=FLAG, confirm_bar_dt=CONF,
                                          direction=direction, price=price)


def _e_state():
    s = state_store.default_state()
    strategy_mode.apply(s, strategy_mode.MODE_E)
    return s


# ── 1~5. EARLY-PASS ───────────────────────────────────────────────────────
def test_trigger_is_flag_confirm_extreme():
    b = _up_bars()
    assert e_strategy.trigger_price(b, FLAG, CONF, Direction.UP_RED) == 2_012_000.0
    assert e_strategy.trigger_price(b, FLAG, CONF, Direction.DOWN_BLUE) == 1_999_000.0


def test_immediate_entry_when_all_three_conditions_hold():
    d = _early(_up_bars(), price=2_010_000.0)          # dist 0.0995%
    assert d.evaluated and d.c1 and d.c2 and d.c3 and d.immediate
    assert not d.upfast                                  # 0.2% 이내라 기존 EARLY-PASS


def test_upfast_widens_distance_only_for_up():
    up = _early(_up_bars(), price=2_006_500.0)           # dist ~0.274%
    assert up.immediate and up.upfast
    # DOWN 은 0.2% 그대로: trigger 1,999,000 에서 0.25% 위면 대기
    dn_bars = _bars([
        (FLAG, 2_010_000.0, 2_011_000.0, 1_999_000.0, 2_004_000.0),
        (CONF, 2_004_000.0, 2_005_000.0, 2_000_000.0, 2_001_000.0),
    ])
    dn = _early(dn_bars, price=1_999_000.0 * 1.0025, direction=Direction.DOWN_BLUE)
    assert not dn.c1 and not dn.immediate


def test_confirm_bar_against_direction_never_enters_immediately():
    """c2=False 폐기조건은 거리가 아무리 가까워도 풀리지 않는다 (10/02 09:54 계열)."""
    d = _early(_up_bars(confirm_body_up=False), price=2_012_000.0)
    assert d.c1 and not d.c2 and not d.immediate and d.trigger == 2_012_000.0


def test_shrinking_gap_waits():
    d = _early(_up_bars(widen=False), price=2_011_900.0)
    assert not d.c3 and not d.immediate


def test_missing_bars_or_price_is_not_evaluated():
    assert not _early(None, price=2_000_000.0).evaluated
    assert not _early(_up_bars(), price=None).evaluated


# ── 6~9. 돌파 대기 ────────────────────────────────────────────────────────
def test_pending_expires_after_wait_minutes_and_on_next_day():
    s = _e_state()
    rec = e_strategy.arm_pending(s, direction=Direction.UP_RED, trigger=2_012_000.0,
                                 signal_id="X:TW2_3SLOT_CONFIRM", flag_bar_dt=FLAG,
                                 confirm_bar_dt=CONF, now=T0, gate={"session": "MORNING"})
    w = float(config.E_WAIT_MINUTES)
    assert e_strategy.pending_expiry_reason(rec, T0 + timedelta(minutes=w)) is None
    assert e_strategy.pending_expiry_reason(rec, T0 + timedelta(minutes=w, seconds=20)) \
        == e_strategy.PENDING_EXPIRED
    assert e_strategy.pending_expiry_reason(rec, T0 + timedelta(days=1)) \
        == e_strategy.PENDING_EXPIRED_DATE_ROLLOVER


def test_new_pending_supersedes_the_old_one():
    s = _e_state()
    e_strategy.arm_pending(s, direction=Direction.UP_RED, trigger=1.0, signal_id="A",
                           flag_bar_dt=FLAG, confirm_bar_dt=CONF, now=T0)
    e_strategy.arm_pending(s, direction=Direction.DOWN_BLUE, trigger=2.0, signal_id="B",
                           flag_bar_dt=FLAG, confirm_bar_dt=CONF, now=T0)
    assert e_strategy.pending_record(s)["signal_id"] == "B"


def test_breakout_hit_direction():
    up = {"direction": Direction.UP_RED.value, "trigger": 100.0}
    dn = {"direction": Direction.DOWN_BLUE.value, "trigger": 100.0}
    assert e_strategy.breakout_hit(up, 100.0) and not e_strategy.breakout_hit(up, 99.0)
    assert e_strategy.breakout_hit(dn, 100.0) and not e_strategy.breakout_hit(dn, 101.0)
    assert not e_strategy.breakout_hit(up, None)


def test_breakout_signal_id_differs_from_approval_id():
    rec = {"signal_id": "20260922_090000_UP_RED:TW2_3SLOT_CONFIRM"}
    fid = e_strategy.breakout_signal_id(rec)
    assert fid != rec["signal_id"] and fid.endswith(e_strategy.BREAKOUT_SUFFIX)


# ── 10~14. RS125 ─────────────────────────────────────────────────────────
def _samples(s, day, n, base=0.0):
    for i in range(n):
        e_strategy.note_rs_sample(s, day, {"atr60": 0.2 + base + i * 0.001,
                                           "min_open": 30.0 + i, "xc30": 3.0 + (i % 3)})


def test_rs_stats_use_only_prior_days():
    s = _e_state()
    _samples(s, "20260918", 30)
    _samples(s, "20260922", 30, base=10.0)               # 오늘 표본 -- 통계에 들어가면 안 된다
    st = e_strategy.rs_stats_for_day(s, "20260922")
    assert st is not None and st["n"] == 30 and st["mu"]["atr60"] < 1.0


def test_rs_needs_minimum_prior_samples():
    s = _e_state()
    _samples(s, "20260918", int(config.E_RS_MIN_SAMPLES) - 1)
    assert e_strategy.rs_stats_for_day(s, "20260922") is None
    rs = e_strategy.evaluate_rs(s, features={"atr60": 1, "min_open": 0, "xc30": 9}, day="20260922")
    assert not rs.hit and rs.multiplier == 1.0 and rs.reason == "NO_STATS"


def test_rs_features_require_today_bars():
    c = np.linspace(2_000_000, 2_010_000, 200)
    assert e_strategy.rs_features(c, T0, today_bars=29) is None
    f = e_strategy.rs_features(c, T0, today_bars=30)
    assert f is not None and f["min_open"] == 6.0
    assert e_strategy.rs_features(c[:119], T0, today_bars=60) is None


def test_rs_boost_is_clipped_to_daily_room_and_recorded():
    s = _e_state()
    cap = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP)
    s.x2lite_exposure_used_today = cap - 1.1
    hit = e_strategy.RsDecision(active=True, hit=True, score=3.0, threshold=2.0, samples=40,
                                multiplier=float(config.E_RS_MULT), reason="RS_HIT")
    b = e_strategy.apply_rs_boost(s, 1.0, hit)
    assert b.capped and b.applied == pytest.approx(1.1) and b.extra == pytest.approx(0.1)
    s.x2lite_exposure_used_today = cap - 1.1 + 1.0        # note_entry 가 base 1.0 을 기록한 상태
    e_strategy.note_boost(s, b)
    assert s.x2lite_exposure_used_today == pytest.approx(cap)
    miss = dataclass_replace(hit, hit=False, multiplier=1.0)
    assert e_strategy.apply_rs_boost(s, 1.0, miss).extra == 0.0


def dataclass_replace(obj, **kw):
    import dataclasses
    return dataclasses.replace(obj, **kw)


def test_rs_table_path_takes_precedence(tmp_path, monkeypatch):
    p = tmp_path / "rs.json"
    p.write_text('{"20260922": {"mu": {"atr60": 0, "min_open": 0, "xc30": 0}, '
                 '"sd": {"atr60": 1, "min_open": 1, "xc30": 1}, "thr": 0.5, "n": 99}}',
                 encoding="utf-8")
    monkeypatch.setattr(config, "E_RS_TABLE_PATH", str(p))
    monkeypatch.delattr(e_strategy._table_from_path, "_cache", raising=False)
    s = _e_state()
    rs = e_strategy.evaluate_rs(s, features={"atr60": 1.0, "min_open": 0.0, "xc30": 0.0},
                                day="20260922")
    assert rs.hit and rs.samples == 99
    monkeypatch.delattr(e_strategy._table_from_path, "_cache", raising=False)


# ── 15~18. 모드 / 상태 ────────────────────────────────────────────────────
def test_e_mode_runs_on_the_full_p3_stack():
    f = strategy_mode.derive(strategy_mode.MODE_E)
    p3 = strategy_mode.derive(strategy_mode.MODE_P3)
    assert f["E"] and not p3["E"]
    assert {k: v for k, v in f.items() if k != "E"} == {k: v for k, v in p3.items() if k != "E"}
    assert strategy_mode.is_p3_based(strategy_mode.MODE_E)
    assert not strategy_mode.is_p3_based(strategy_mode.MODE_N1)


@pytest.mark.parametrize("mode", [strategy_mode.MODE_N1, strategy_mode.MODE_P3])
def test_other_modes_are_inert(mode):
    s = state_store.default_state()
    strategy_mode.apply(s, mode)
    assert not e_strategy.is_active(s)
    assert e_strategy.evaluate_rs(s, features={"atr60": 1}, day="20260922") is e_strategy.NEUTRAL_RS


def test_leaving_e_clears_the_pending_breakout_but_keeps_rs_samples():
    s = _e_state()
    _samples(s, "20260918", 3)
    e_strategy.arm_pending(s, direction=Direction.UP_RED, trigger=1.0, signal_id="A",
                           flag_bar_dt=FLAG, confirm_bar_dt=CONF, now=T0)
    strategy_mode.apply(s, strategy_mode.MODE_P3)
    assert e_strategy.pending_record(s) is None and len(e_strategy.rs_samples(s)) == 3


def test_restart_restores_e_mode_pending_and_samples():
    s = _e_state()
    _samples(s, "20260918", 2)
    e_strategy.arm_pending(s, direction=Direction.UP_RED, trigger=2_012_000.0, signal_id="A",
                           flag_bar_dt=FLAG, confirm_bar_dt=CONF, now=T0, gate={"session": "MORNING"})
    r = state_store.deserialize(state_store.serialize(s))
    strategy_mode.restore(r)
    assert strategy_mode.current(r) == strategy_mode.MODE_E and e_strategy.is_active(r)
    assert e_strategy.pending_record(r)["trigger"] == 2_012_000.0
    assert e_strategy.pending_record(r)["gate"] == {"session": "MORNING"}
    assert len(e_strategy.rs_samples(r)) == 2


def test_day_rollover_drops_pending_keeps_samples():
    s = _e_state()
    _samples(s, "20260918", 2)
    e_strategy.arm_pending(s, direction=Direction.UP_RED, trigger=1.0, signal_id="A",
                           flag_bar_dt=FLAG, confirm_bar_dt=CONF, now=T0)
    e_strategy.reset_daily(s)
    assert e_strategy.pending_record(s) is None and len(e_strategy.rs_samples(s)) == 2


# ── 19~22. worker 보조 ───────────────────────────────────────────────────
class _Res:
    def __init__(self):
        self.actions = []
        self.signal_dispatch_trace = {}


def test_advance_pending_is_noop_outside_e():
    s = state_store.default_state()
    strategy_mode.apply(s, strategy_mode.MODE_P3)
    s.e_pending = {"signal_id": "A", "direction": "UP_RED", "trigger": 1.0}   # 남은 레코드가 있어도
    out = worker._advance_e_pending(broker=None, market_data=None, state=s, now=T0,
                                    macd_snap=None, bars_3m=None, df_1m=None,
                                    position=None, result=_Res())
    assert out is None and s.e_pending is not None


def test_expired_pending_is_cleared_by_worker():
    s = _e_state()
    e_strategy.arm_pending(s, direction=Direction.UP_RED, trigger=1.0, signal_id="A",
                           flag_bar_dt=FLAG, confirm_bar_dt=CONF, now=T0)
    r = _Res()
    out = worker._advance_e_pending(broker=None, market_data=None, state=s,
                                    now=T0 + timedelta(minutes=16), macd_snap=None, bars_3m=None,
                                    df_1m=None, position=None, result=r)
    assert out is None and e_strategy.pending_record(s) is None
    assert s.e_last_pending_result == e_strategy.PENDING_EXPIRED
    assert any(a.startswith("E_PENDING_EXPIRED") for a in r.actions)


@pytest.mark.parametrize("setup,reason", [
    (lambda s: setattr(s, "auto_trade_on", False), "AUTO_TRADE_OFF"),
    (lambda s: setattr(s, "tw2_3slot_slots_used_today", int(config.TW2_3SLOT_DAILY_CAP)),
     "TW2_3SLOT_DAILY_CAP_REACHED"),
    (lambda s: setattr(s, "processed_signal_ids", ["A" + e_strategy.BREAKOUT_SUFFIX]),
     "DUPLICATE_SIGNAL_ID"),
])
def test_fire_hard_safety_blocks(setup, reason):
    s = _e_state()
    s.auto_trade_on, s.stopped = True, False
    setup(s)
    assert worker._e_fire_hard_safety(s, T0, {"signal_id": "A"}) == reason


def test_fire_hard_safety_respects_new_entry_cutoff():
    s = _e_state()
    s.auto_trade_on, s.stopped = True, False
    late = T0.replace(hour=config.NEW_ENTRY_CUTOFF.hour, minute=config.NEW_ENTRY_CUTOFF.minute)
    assert worker._e_fire_hard_safety(s, late, {"signal_id": "A"}) == "NEW_ENTRY_CUTOFF"
    assert worker._e_fire_hard_safety(s, T0, {"signal_id": "A"}) is None


def test_rs_features_count_only_today_bars():
    prev = pd.date_range("2026-09-19 13:00", periods=150, freq="1min", tz=KST)
    today = pd.date_range("2026-09-22 08:00", periods=29, freq="1min", tz=KST)
    df = pd.DataFrame({"datetime": prev.append(today), "close": np.linspace(1, 2, 179) * 1e6})
    now = datetime(2026, 9, 22, 8, 29, 30, tzinfo=KST)
    assert worker._e_rs_features(df, now) is None                 # 당일 29봉
    df2 = pd.concat([df, pd.DataFrame({"datetime": [today[-1] + timedelta(minutes=1)],
                                       "close": [2e6]})], ignore_index=True)
    assert worker._e_rs_features(df2, now + timedelta(minutes=1)) is not None


# ── 23. 반대 포지션 보유 중 대기 등록 -> 반대 포지션은 지금 청산된다 ─────────
def test_arming_with_opposite_position_still_exits_it():
    """2026-10-06 parity 06/15·06/25: 대기 등록이 signal_id 를 먼저 processed 로
    찍으면 같은 id 로 내는 반대 포지션 청산이 '중복'으로 막혀 롱을 계속 들고 있었다.
    반대 포지션이 있을 때는 processed 를 청산 함수가 찍어야 한다."""
    from types import SimpleNamespace

    from app.trading.macd2.models import MajorFlagDecision, PositionSnapshot, SignalState
    from tests.macd2.fake_broker import FakeBroker

    s = _e_state()
    sid = "20260615_095100_DOWN_BLUE:TW2_3SLOT_CONFIRM"
    early = SimpleNamespace(trigger=2_301_000.0, dist_pct=0.43, c1=False, c2=False, c3=True)
    from app.trading.macd2.models import MacdSnapshot
    snap = MacdSnapshot(bar_dt=T0, macd=-1.0, signal=0.0, hist=-1.0, hist_last3=(0.5, -0.5, -1.0),
                        completed_3m_count=100, previous_diff=0.5, current_diff=-1.0,
                        relation="BELOW")
    assert worker._e_arm_pending_instead_of_entry(
        state=s, now=T0, macd_snap=snap, direction=Direction.DOWN_BLUE, signal_id=sid,
        flag_bar_dt=FLAG, price=2_311_000.0, early=early, slot_metrics={}, gate={},
        result=_Res(), mark_processed=False)
    assert sid not in s.processed_signal_ids and e_strategy.pending_record(s) is not None

    broker = FakeBroker(cash=0.0, quotes={config.LONG_SYMBOL: 27_600.0})
    broker._positions[config.LONG_SYMBOL] = __import__("app.models", fromlist=["Position"]).Position(
        symbol=config.LONG_SYMBOL, name=config.LONG_SYMBOL, quantity=385, avg_price=27_035.0,
        current_price=27_600.0)
    pos = PositionSnapshot(symbol=config.LONG_SYMBOL, quantity=385, avg_price=27_035.0)
    dec = MajorFlagDecision(approved=False, score=0.0, required_score=0.0,
                            decision="E_PENDING_BREAKOUT", reasons=("e",), component_scores={},
                            metrics={}, is_reversal=True, fast_reversal=False,
                            block_reason="E_PENDING_BREAKOUT")
    out = worker._execute_reversal_exit_only_for_filtered_entry(
        broker=broker, state=s, macd_snap=snap, direction=Direction.DOWN_BLUE, position=pos,
        decision=dec, result=_Res(), gate_mode="TW2_3SLOT", signal_id_override=sid)
    assert out is not None and out.final_state == SignalState.EXECUTED
    assert sid in s.processed_signal_ids
    assert any(o.side == "SELL" and o.symbol == config.LONG_SYMBOL for o in broker.orders)


def test_arming_without_opposite_position_marks_processed():
    from types import SimpleNamespace

    s = _e_state()
    sid = "X:TW2_3SLOT_CONFIRM"
    early = SimpleNamespace(trigger=1.0, dist_pct=0.5, c1=False, c2=True, c3=True)
    assert worker._e_arm_pending_instead_of_entry(
        state=s, now=T0, macd_snap=SimpleNamespace(bar_dt=T0), direction=Direction.UP_RED,
        signal_id=sid, flag_bar_dt=FLAG, price=1.0, early=early, slot_metrics={}, gate={},
        result=_Res())
    assert sid in s.processed_signal_ids
