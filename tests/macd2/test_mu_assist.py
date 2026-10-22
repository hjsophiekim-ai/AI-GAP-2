"""MU ASSIST (MACD2_MU_ASSIST_ENABLED, 2026-10-22) — worker.run_once() 경로 테스트.

고정하는 계약
-------------
0. OFF 면 훅이 한 번도 호출되지 않는다(원장 파일도 없다) — 기존 동작 그대로
1. ON 진입 보조: T+2 창에서 ENTRY_SHADOW 1행, 약하면 ENTRY_CONFIRM_WEAK 경고만, T+3 진입은 그대로
2. ON H50 gap 청산: H50 HOLD 대상 반대플래그 + T+2 gap 확대 -> T+2 에 매도 1건, T+3 반대매수 없음
3. gap 이 확대되지 않으면 아무것도 안 하고 T+3 에서 H50 HOLD 가 예전 그대로
4. MU 결측/stale 이면 기록만 MISSING/STALE — 판정은 기존 그대로(청산 조건은 MU 가 아니다)
5. H50 이 없는 모드(X2-lite 단독)에서는 no-op
6. P3 / E / CHOP / N1 각각에서 같은 토글로 동작
7. 두 기능 동시 — 같은 tick 에 진입 기록과 청산이 함께, 중복주문 0
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.trading.macd2 import config, ledger, mu_assist, state_store, strategy_mode, worker
from app.trading.macd2 import small_whipsaw_hold as swh
from app.trading.macd2 import time_window_3slot as tw3
from app.trading.macd2.models import Direction, PositionSnapshot, RuntimeState
from app.trading.macd2.worker import run_once
from tests.macd2.fake_broker import FakeBroker
from tests.macd2.test_tw2_3slot_worker_regression import (  # noqa: F401  (fixture)
    _approved,
    _patch_common,
    _quality,
    _teg,
    tw2_3slot_market_data,
)

KST = config.KST


# ── helpers ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_mu_assist():
    mu_assist.reset_for_tests()
    yield
    mu_assist.reset_for_tests()


def _write_mu(root: Path, day: datetime, closes_fn, *, until: datetime | None = None) -> None:
    """로거와 같은 mu/<YYYY-MM-DD>/bars_1m.csv. 08:00~08:59 DNASMU, 09:00~ RBAQMU."""
    d = root / "mu" / day.strftime("%Y-%m-%d")
    d.mkdir(parents=True, exist_ok=True)
    start = day.replace(hour=8, minute=0, second=0, microsecond=0, tzinfo=None)
    end = (until.replace(tzinfo=None) if until is not None else start + timedelta(hours=6))
    with open(d / "bars_1m.csv", "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["minute_kst", "feed", "open", "high", "low", "close", "volume", "ticks",
                    "first_kst", "last_kst", "first_recv", "last_recv", "complete"])
        m, i = start, 0
        while m < end:
            c = closes_fn(i, m)
            feed = "DNASMU" if m.hour < 9 else "RBAQMU"
            w.writerow([m.isoformat(timespec="seconds"), feed, c, c, c, c, 10, 3, "", "", "", "", 1])
            m += timedelta(minutes=1)
            i += 1


def _rising(i, m):           # MU 가 계속 오른다 (RED 쪽으로 강함)
    return 900.0 + 0.05 * i + 0.002 * i * i


def _falling(i, m):          # MU 가 계속 내린다 (BLUE 쪽, RED 보유에 반대)
    return 900.0 - 0.05 * i - 0.002 * i * i


def _on(monkeypatch, tmp_path, closes_fn=_rising, **kw):
    monkeypatch.setattr(config, "MU_ASSIST_ENABLED", True)
    if closes_fn is None:
        monkeypatch.setattr(config, "MU_ASSIST_DATA_DIR", str(tmp_path / "no_logger"))
        return
    _write_mu(tmp_path / "logger", kw.pop("day"), closes_fn, **kw)
    monkeypatch.setattr(config, "MU_ASSIST_DATA_DIR", str(tmp_path / "logger"))


def _mode_state(mode: str) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    strategy_mode.apply(state, mode)
    return state


def _legacy_state(flag: str) -> RuntimeState:
    state = state_store.default_state()
    state.auto_trade_on = True
    state.budget = 10_000_000.0
    for f in ("time_window_2_filter_enabled", "time_window_teg_filter_enabled",
              "time_window_3slot_filter_enabled", "time_window_twf_filter_enabled",
              "time_window_x2lite_filter_enabled", "time_window_h50_filter_enabled",
              "time_window_n1_filter_enabled"):
        setattr(state, f, f == flag)
    return state


def _broker() -> FakeBroker:
    return FakeBroker(cash=10_000_000.0,
                      quotes={config.LONG_SYMBOL: 15_000.0, config.INVERSE_SYMBOL: 10_000.0})


def _seed_inverse(state: RuntimeState, broker: FakeBroker, now: datetime, qty: int = 10) -> None:
    state.position = PositionSnapshot(symbol=config.INVERSE_SYMBOL, quantity=qty,
                                      avg_price=10_000.0, entry_at=now - timedelta(minutes=30))
    state.time_window_position_active = True
    state.time_window_active_mode = tw3.active_3slot_mode(state) or tw3.MODE_N1_3SLOT
    state.time_window_entry_session = "MORNING"
    state.tw2_3slot_slots_used_today = 1
    state.tw2_3slot_morning_count = 1
    broker.buy_market(config.INVERSE_SYMBOL, qty, "seed-order")
    broker._positions[config.INVERSE_SYMBOL].avg_price = 10_000.0


def _hold(should_hold: bool) -> swh.HoldDecision:
    return swh.HoldDecision(should_hold=should_hold, trend=swh.TREND_DOWN, range_pct=1.2,
                            trend_ok=should_hold, range_ok=should_hold, insufficient_data=False,
                            reason="SMALL_WHIPSAW_HOLD" if should_hold else "RANGE_TOO_WIDE")


def _patch(monkeypatch, *, hold: bool = True, t2_gap: float | None = 1e9):
    """T+3 게이트(승인) / H50 HOLD 판정 / T+2 형성봉 gap 을 고정한다.
    t2_gap=None 이면 형성봉 계산을 production 그대로 둔다."""
    _patch_common(monkeypatch, entry_decision=_approved(), quality_decision=_quality(True, 5),
                  teg_decision=_teg(True))
    monkeypatch.setattr(worker.small_whipsaw_hold, "evaluate_hold", lambda *a, **kw: _hold(hold))
    monkeypatch.setattr(worker.small_whipsaw_hold, "evaluate_release",
                        lambda *a, **kw: swh.ReleaseDecision(False, swh.TREND_DOWN, 0, 1.0, "HOLDING"))
    if t2_gap is not None:
        monkeypatch.setattr(worker, "calculate_provisional_macd",
                            lambda *a, **kw: SimpleNamespace(current_diff=t2_gap))


def _prime(state: RuntimeState, flag_bar: datetime, direction: Direction = Direction.UP_RED) -> None:
    state.tw2_3slot_pending_flag_direction = direction
    state.tw2_3slot_pending_flag_bar_ts = flag_bar.isoformat()


def _t2(flag_bar: datetime) -> datetime:
    return flag_bar + timedelta(minutes=5, seconds=20)


def _t3(flag_bar: datetime) -> datetime:
    return flag_bar + timedelta(minutes=6, seconds=5)


def _mu_rows() -> list[dict]:
    p = mu_assist.ledger_path()
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def _sells(broker) -> list:
    return [o for o in broker.orders if o.side == "SELL" and o.success]


def _buys_after_seed(broker) -> list:
    return [o for o in broker.orders if o.side == "BUY" and o.success][1:]


def _signal_rows() -> list[dict]:
    return ledger.load_signal_ledger(limit=0)


# ── 0. OFF ────────────────────────────────────────────────────────────────

def test_default_is_off():
    assert config.MU_ASSIST_ENABLED is False


def test_off_never_calls_any_hook_and_h50_holds_as_before(tw2_3slot_market_data, monkeypatch):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True)

    def _boom(*_a, **_kw):
        raise AssertionError("OFF 인데 MU ASSIST 훅이 호출됐다")

    monkeypatch.setattr(worker, "_advance_mu_assist_entry_shadow", _boom)
    monkeypatch.setattr(worker, "_advance_mu_assist_h50_gap_exit", _boom)
    monkeypatch.setattr(worker.mu_assist, "exited_on_flag", _boom)
    _prime(state, flag)

    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    result = run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))

    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in result.actions), result.actions
    assert state.position is not None and state.position.symbol == config.INVERSE_SYMBOL
    assert _sells(broker) == []
    assert not mu_assist.ledger_path().exists(), "OFF 인데 MU ASSIST 원장이 생겼다"


def _scenario(svc, state, broker, flag):
    _prime(state, flag)
    a1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag)).actions
    a2 = run_once(broker=broker, market_data=svc, state=state, now=_t3(flag)).actions
    return list(a1) + list(a2)


def test_on_without_trigger_is_identical_to_off(tw2_3slot_market_data, monkeypatch, tmp_path):
    """ON 이어도 gap 이 확대되지 않으면 주문·액션·포지션이 OFF 와 같다."""
    from tests.macd2.test_h50_hold_lifecycle import _make_market_data, _reset_dispatch_history

    svc_a, now0 = _make_market_data()
    flag = now0
    st_a, br_a = _mode_state(strategy_mode.MODE_N1), _broker()
    _seed_inverse(st_a, br_a, flag)
    _patch(monkeypatch, hold=True, t2_gap=-1e9)
    off_actions = _scenario(svc_a, st_a, br_a, flag)

    _reset_dispatch_history()
    svc_b, _ = _make_market_data()
    st_b, br_b = _mode_state(strategy_mode.MODE_N1), _broker()
    _seed_inverse(st_b, br_b, flag)
    _on(monkeypatch, tmp_path, day=flag)
    on_actions = _scenario(svc_b, st_b, br_b, flag)

    legs = lambda b: [(o.symbol, o.side, o.requested_qty, o.executed_qty, o.success) for o in b.orders]
    assert on_actions == off_actions
    assert legs(br_b) == legs(br_a)
    assert st_b.position.symbol == st_a.position.symbol == config.INVERSE_SYMBOL
    assert st_b.h50_hold_active is True and st_a.h50_hold_active is True


# ── 1. 진입 보조 ──────────────────────────────────────────────────────────

def test_entry_shadow_records_once_and_does_not_change_the_entry(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _patch(monkeypatch)
    _on(monkeypatch, tmp_path, _rising, day=flag)
    _prime(state, flag, Direction.UP_RED)

    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag) + timedelta(seconds=20))
    rows = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_ENTRY]
    assert len(rows) == 1, rows
    r = rows[0]
    assert r["mu_t2_status"] == mu_assist.STATUS_OK
    assert r["mu_same_direction"] == "True"
    assert float(r["mu_post2_return_bp"]) > 0
    assert r["mu_entry_warning"] == ""
    for col in ("mu_same_direction", "mu_post2_return_bp", "mu_acc2", "mu_t2_status", "mu_entry_warning"):
        assert col in r

    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    assert len([o for o in broker.orders if o.side == "BUY" and o.success]) == 1, "T+3 진입은 그대로여야 한다"


def test_weak_mu_only_warns_never_blocks(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _patch(monkeypatch)
    _on(monkeypatch, tmp_path, _falling, day=flag)      # RED 플래그에 MU 반대
    _prime(state, flag, Direction.UP_RED)

    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    r = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_ENTRY][0]
    assert r["mu_entry_warning"] == config.MU_ASSIST_ENTRY_WARNING
    assert r["mu_same_direction"] == "False"

    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    assert len([o for o in broker.orders if o.side == "BUY" and o.success]) == 1, "경고만 -- hard block 금지"


# ── 2/3. H50 gap 청산 ─────────────────────────────────────────────────────

def test_gap_expanding_exits_at_t2_and_never_buys_the_opposite(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, tmp_path, _rising, day=flag)
    _prime(state, flag)

    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert any(a.startswith(config.EXIT_MU_ASSIST_H50_GAP) for a in r1.actions), r1.actions
    assert state.position is None
    assert state.h50_hold_active is False
    # 같은 창에서 다시 돌아도 추가 매도 없음
    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag) + timedelta(seconds=20))
    assert len(_sells(broker)) == 1
    # T+3 승인이어도 반대 ETF 매수 없음
    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    assert _buys_after_seed(broker) == [], broker.orders
    blocked = [r for r in _signal_rows() if r.get("block_reason") == config.MU_ASSIST_NO_REENTRY_BLOCK_REASON]
    assert len(blocked) == 1, [(r.get("signal_id"), r.get("block_reason")) for r in _signal_rows()]
    ex = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_H50_EXIT]
    assert len(ex) == 1 and ex[0]["exit_result"] == "EXECUTED"
    assert ex[0]["mu_opposite"] == "True"           # MU 상승 = 인버스(BLUE) 보유의 반대방향 — 보조로그만
    exec_rows = ledger.load_execution_ledger(limit=0)
    assert [r["exit_reason"] for r in exec_rows if r.get("side") == "SELL"] == [config.EXIT_MU_ASSIST_H50_GAP]


def test_gap_not_expanding_keeps_the_h50_hold(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=-1e9)
    _on(monkeypatch, tmp_path, _rising, day=flag)
    _prime(state, flag)

    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert not any(config.EXIT_MU_ASSIST_H50_GAP in a for a in r1.actions)
    r2 = run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    assert any(a.startswith(config.H50_HOLD_BLOCK_REASON) for a in r2.actions), r2.actions
    assert state.position is not None and _sells(broker) == []
    kept = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_H50_KEPT]
    assert len(kept) == 1 and kept[0]["hy_gap_expanding"] == "False"


def test_flag_that_h50_would_not_hold_is_left_to_the_t3_path(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=False, t2_gap=1e9)
    _on(monkeypatch, tmp_path, _rising, day=flag)
    _prime(state, flag)

    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert not any(config.EXIT_MU_ASSIST_H50_GAP in a for a in r1.actions)
    assert state.position is not None


def test_opposite_flag_alone_outside_the_t2_window_never_exits(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, tmp_path, _rising, day=flag)
    _prime(state, flag)
    for t in (flag + timedelta(minutes=3, seconds=20), flag + timedelta(minutes=4, seconds=20),
              flag + timedelta(minutes=5, seconds=2)):
        r = run_once(broker=broker, market_data=svc, state=state, now=t)
        assert not any(config.EXIT_MU_ASSIST_H50_GAP in a for a in r.actions), (t, r.actions)
    assert state.position is not None


# ── 4. MU 결측 / stale ────────────────────────────────────────────────────

def test_mu_missing_records_missing_and_exit_still_follows_hynix_gap(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, tmp_path, None)
    _prime(state, flag)

    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert any(a.startswith(config.EXIT_MU_ASSIST_H50_GAP) for a in r1.actions)
    entry = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_ENTRY][0]
    assert entry["mu_t2_status"] == mu_assist.STATUS_MISSING and entry["mu_entry_warning"] == ""
    ex = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_H50_EXIT][0]
    assert ex["mu_t2_status"] == mu_assist.STATUS_MISSING


def test_mu_stale_falls_back_without_warning(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_N1)
    broker = _broker()
    _patch(monkeypatch)
    _on(monkeypatch, tmp_path, _falling, day=flag, until=flag)     # 로거가 플래그봉 전에 멈춤
    _prime(state, flag, Direction.UP_RED)

    run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    r = [r for r in _mu_rows() if r["kind"] == mu_assist.KIND_ENTRY][0]
    assert r["mu_t2_status"] == mu_assist.STATUS_STALE
    assert r["mu_entry_warning"] == ""
    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    assert len([o for o in broker.orders if o.side == "BUY" and o.success]) == 1


# ── 5. H50 없는 모드 ──────────────────────────────────────────────────────

def test_mode_without_h50_is_a_noop(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _legacy_state("time_window_x2lite_filter_enabled")
    assert swh.is_active(state) is False
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, tmp_path, _rising, day=flag)
    _prime(state, flag)

    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert not any(config.EXIT_MU_ASSIST_H50_GAP in a for a in r1.actions)
    assert state.position is not None
    assert not [r for r in _mu_rows() if r["kind"] in (mu_assist.KIND_H50_EXIT, mu_assist.KIND_H50_KEPT)]


# ── 6. 전략 모드별 / 7. 동시 ON ───────────────────────────────────────────

@pytest.mark.parametrize("mode", [strategy_mode.MODE_P3, strategy_mode.MODE_E,
                                  strategy_mode.MODE_CHOP, strategy_mode.MODE_N1])
def test_every_strategy_mode_uses_the_same_toggle(tw2_3slot_market_data, monkeypatch, tmp_path, mode):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(mode)
    assert swh.is_active(state) is True, f"{mode} 에서 H50 이 꺼져 있다"
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, tmp_path, _rising, day=flag)
    _prime(state, flag)

    r1 = run_once(broker=broker, market_data=svc, state=state, now=_t2(flag))
    assert any(a.startswith(config.EXIT_MU_ASSIST_H50_GAP) for a in r1.actions), (mode, r1.actions)
    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    assert len(_sells(broker)) == 1 and _buys_after_seed(broker) == [], (mode, broker.orders)
    kinds = sorted(r["kind"] for r in _mu_rows())
    assert kinds == sorted([mu_assist.KIND_ENTRY, mu_assist.KIND_H50_EXIT]), (mode, kinds)


def test_both_features_on_same_tick_one_order(tw2_3slot_market_data, monkeypatch, tmp_path):
    svc, now0 = tw2_3slot_market_data
    flag = now0
    state = _mode_state(strategy_mode.MODE_CHOP)
    broker = _broker()
    _seed_inverse(state, broker, flag)
    _patch(monkeypatch, hold=True, t2_gap=1e9)
    _on(monkeypatch, tmp_path, _rising, day=flag)
    _prime(state, flag)

    for k in range(3):
        run_once(broker=broker, market_data=svc, state=state, now=_t2(flag) + timedelta(seconds=10 * k))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag))
    run_once(broker=broker, market_data=svc, state=state, now=_t3(flag) + timedelta(seconds=20))
    assert len(_sells(broker)) == 1
    assert _buys_after_seed(broker) == []
    rows = _mu_rows()
    assert [r["kind"] for r in rows].count(mu_assist.KIND_ENTRY) == 1
    assert [r["kind"] for r in rows].count(mu_assist.KIND_H50_EXIT) == 1


# ── 순수 함수 ────────────────────────────────────────────────────────────

def test_t2_window_bounds():
    f = datetime(2026, 10, 22, 9, 3, tzinfo=KST)
    assert not mu_assist.in_t2_window(f, f + timedelta(minutes=5, seconds=5))
    assert mu_assist.in_t2_window(f, f + timedelta(minutes=5, seconds=10))
    assert mu_assist.in_t2_window(f, f + timedelta(minutes=5, seconds=59))
    assert not mu_assist.in_t2_window(f, f + timedelta(minutes=6))


def test_entry_metrics_match_the_research_definition(tmp_path):
    """10/22 09:03 RED 와 같은 모양: 플래그봉에 크게 오르고 T+2 에 멈춤 -> acc2 < 0, 경고."""
    day = datetime(2026, 10, 22, 8, 0)
    flag = datetime(2026, 10, 22, 9, 3, tzinfo=KST)

    def shape(i, m):
        if m < datetime(2026, 10, 22, 9, 3):
            return 900.0 + 0.01 * i
        if m < datetime(2026, 10, 22, 9, 6):
            return 900.6 + 0.9 * (m.minute - 3 + 1)       # 플래그봉 +29bp 급등
        return 903.3                                       # T+2 까지 멈춤
    _write_mu(tmp_path, day, shape)
    s = mu_assist.load_mu_1m(flag, str(tmp_path))
    out = mu_assist.evaluate_entry(s, Direction.UP_RED, flag)
    assert out["mu_t2_status"] == mu_assist.STATUS_OK
    assert out["mu_pre3_return_bp"] > 20
    assert abs(out["mu_post2_return_bp"]) < 1e-6
    assert out["mu_acc2"] < 0
    assert out["mu_entry_warning"] == config.MU_ASSIST_ENTRY_WARNING
