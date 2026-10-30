"""P70R SHADOW (2026-10-30) — 기록 전용.

실제 손절은 100% 전량 그대로이고(주문 수량/시각/사유 불변), N1 오전 손절에서 70/30·50/50
분할손절을 했다면의 결과만 가상으로 쌓이는지 본다.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.trading.macd2 import config, ledger, p70r_shadow as ps, strategy_mode
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.macd2.models import SignalState

KST = config.KST
SYM = config.INVERSE_SYMBOL
T0 = datetime(2026, 10, 29, 9, 15, 5, tzinfo=KST)    # 손절 판정 tick (09:15 봉 시작 직후)


class _Outcome:
    def __init__(self, order_id="S1", now=T0):
        self.sell_result = BrokerOrderResult(True, order_id, SYM, "SELL", 100, 100, 0.0, "OK")
        self.final_state = SignalState.EXECUTED
        self.timestamps = {"sell_requested_at": now.isoformat(), "sell_confirmed_at": (now + timedelta(seconds=1)).isoformat(),
                           "sell_reconciled_at": (now + timedelta(seconds=2)).isoformat()}


def _ticks(prices, end=T0, step=10):
    """end 직전까지 step 초 간격 tick 을 observe 로 넣는다(마지막 = end − step)."""
    n = len(prices)
    for i, p in enumerate(prices):
        ps.observe(end - timedelta(seconds=step * (n - i)), {SYM: p})


def _write_sell_leg(order_id, price, source="ACTUAL_FILL"):
    ledger.append_execution({"order_id": order_id, "side": "SELL", "symbol": SYM, "executed_qty": 100,
                             "executed_price": price, "timestamp": T0.isoformat(), "price_source": source})


def _stop(gate_price, *, order_id="S1", entry=10_000.0, ticks=None, sell_px=None):
    _ticks(ticks if ticks is not None else [9_900.0] * 20)
    g = ps.gate(SYM, T0, gate_price)
    _write_sell_leg(order_id, sell_px if sell_px is not None else gate_price)
    ps.record_stop(now=T0, symbol=SYM, qty=100, entry_price=entry, stop_pct=-1.3, bar_net_pct=-1.4,
                   decision_price=gate_price, gate_info=g, outcome=_Outcome(order_id), exit_reason=config.EXIT_TW_STOP_LOSS,
                   strategy_mode="N1_3SLOT")
    return g


def _drive(path):
    """[(초, 가격)] — T0 기준 초 뒤 tick 들."""
    for sec, px in path:
        ps.update(T0 + timedelta(seconds=sec), {SYM: px})


def _row():
    rs = ps.rows()
    assert len(rs) == 1
    return rs[0]


# ── 게이트 ─────────────────────────────────────────────────────────────────
def test_gate_unavailable_without_three_minutes_of_ticks():
    ps.observe(T0 - timedelta(seconds=20), {SYM: 9_850.0})
    assert ps.gate(SYM, T0, 9_920.0)["status"] == ps.UNAVAILABLE
    assert ps.gate(SYM, T0, None)["status"] == ps.UNAVAILABLE


def test_gate_pass_and_fail_use_three_minute_tick_low_including_now():
    _ticks([9_900.0] * 10 + [9_850.0] + [9_880.0] * 7)     # 180초 안 저점 9,850
    g = ps.gate(SYM, T0, 9_910.0)                           # +0.61%
    assert g["status"] == ps.PASS and g["low3"] == 9_850.0 and g["rebound_pct"] == pytest.approx(0.609, abs=1e-3)
    assert ps.gate(SYM, T0, 9_905.0)["status"] == ps.FAIL   # +0.56%
    # 180초보다 오래된 tick 은 저점에 쓰지 않는다
    ps.reset_for_tests()
    ps.observe(T0 - timedelta(seconds=300), {SYM: 9_000.0})
    _ticks([9_900.0] * 18)
    assert ps.gate(SYM, T0, 9_910.0)["low3"] == 9_900.0


# ── runner 규칙 ─────────────────────────────────────────────────────────────
def test_runner_take_profit_and_fake_stop_deltas():
    _stop(9_910.0, ticks=[9_850.0] * 18)
    # 진입 10,000 기준 +1.5% 넘는 10,160 에 도달 -> R_TP, 30분 안 +1.5% -> 가짜 손절
    _drive([(30, 9_950.0), (90, 10_100.0), (120, 10_160.0)])
    _drive([(3601, 10_000.0)])
    r = _row()
    assert r["P70R_SHADOW"] == ps.PASS and r["runner_exit_reason"] == ps.R_TP and r["runner_exit_price"] == 10_160.0
    assert r["fake_stop"] is True and r["done"] is True
    a = ps._net_pct(10_000.0, 9_910.0); run = ps._net_pct(10_000.0, 10_160.0)
    assert r["A_pct"] == pytest.approx(a, abs=1e-4)
    assert r["delta_7030_pct"] == pytest.approx(0.3 * (run - a), abs=1e-3)
    assert r["delta_5050_pct"] == pytest.approx(0.5 * (run - a), abs=1e-3)
    assert r["delta_5050_krw"] == pytest.approx(0.5 * (run - a) / 100 * 10_000 * 100, abs=1)
    assert r["mfe_pct"] == pytest.approx(run, abs=1e-4)
    # 원장 SELL 행에 요약 3컬럼
    led = [x for x in ledger.load_execution_ledger() if x["order_id"] == "S1"][0]
    assert led["p70r_shadow"] == "PASS" and float(led["p70r_delta_5050_pct"]) == pytest.approx(r["delta_5050_pct"])


def test_runner_new_low_exits_and_counts_as_real_drop():
    _stop(9_910.0, ticks=[9_850.0] * 18)
    _drive([(20, 9_880.0), (40, 9_840.0)])                 # 3분 저점 9,850 이탈
    _drive([(3601, 9_700.0)])
    r = _row()
    assert r["runner_exit_reason"] == ps.R_NEWLOW and r["runner_exit_price"] == 9_840.0
    assert r["fake_stop"] is False
    assert r["delta_7030_pct"] < 0 and r["delta_5050_pct"] < r["delta_7030_pct"]


def test_runner_stop_on_next_completed_3m_bar_close():
    _stop(9_910.0, ticks=[9_850.0] * 18)
    # 09:15 봉의 마지막 관측가 9,860(−1.43%) -> 09:18 첫 tick 에 R_SL
    _drive([(60, 9_900.0), (165, 9_870.0), (172, 9_860.0), (180, 9_880.0)])
    r = _row()
    assert r["runner_exit_reason"] == ps.R_SL and r["runner_exit_at"] == (T0 + timedelta(seconds=180)).isoformat()


def test_runner_time_exit_after_60_minutes_and_uses_actual_fill_price():
    _stop(9_910.0, ticks=[9_850.0] * 18, sell_px=9_905.0)  # 원장 ACTUAL_FILL 9,905 가 A 가격
    _drive([(s, 9_900.0 if (s // 180) % 2 else 9_960.0) for s in range(10, 3600, 30)])
    _drive([(3600, 9_955.0)])
    r = _row()
    assert r["runner_exit_reason"] == ps.R_TIME and r["sell_price"] == 9_905.0 and r["price_source"] == "ACTUAL_FILL"
    assert r["A_pct"] == pytest.approx(ps._net_pct(10_000.0, 9_905.0), abs=1e-4)


def test_fail_row_has_zero_delta_but_tracks_mfe_mae():
    _stop(9_880.0, ticks=[9_850.0] * 18)                   # +0.30% 반등 -> FAIL
    _drive([(60, 9_700.0), (600, 10_000.0)])
    _drive([(3601, 9_900.0)])
    r = _row()
    assert r["P70R_SHADOW"] == ps.FAIL and r["runner_exit_reason"] == ""
    assert r["delta_7030_pct"] == 0 and r["delta_5050_pct"] == 0
    assert r["mae_pct"] < -2.9 and r["mfe_pct"] is not None and r["fake_stop"] is False


def test_day_rollover_closes_open_runner_with_last_seen_price():
    _stop(9_910.0, ticks=[9_850.0] * 18)
    _drive([(30, 9_930.0)])
    ps.update(T0 + timedelta(days=1), {SYM: 11_000.0})     # 다음날 가격은 쓰지 않는다
    r = _row()
    assert r["runner_exit_reason"] == ps.R_DAY_END and r["runner_exit_price"] == 9_930.0


def test_update_does_not_read_file_when_nothing_open(monkeypatch):
    ps.update(T0, {SYM: 1.0})
    monkeypatch.setattr(ps, "_rows_cached", lambda: (_ for _ in ()).throw(AssertionError("읽으면 안 된다")))
    ps.update(T0 + timedelta(seconds=10), {SYM: 1.0})


def test_summary_counts_and_ex_top():
    rows = []
    for i, (d7, d5, fake, day) in enumerate([(0.9, 1.5, True, "20261029"), (0.3, 0.5, True, "20261030"),
                                             (-0.2, -0.3, False, "20261030"), (0.0, 0.0, None, "20261031")]):
        rows.append({"trading_date": day, "done": True, "P70R_SHADOW": ps.PASS if fake is not None else ps.FAIL,
                     "fake_stop": bool(fake), "A_pct": -1.4, "B7030_pct": -1.4 + d7, "C5050_pct": -1.4 + d5,
                     "delta_7030_pct": d7, "delta_5050_pct": d5, "delta_7030_krw": d7 * 1000, "delta_5050_krw": d5 * 1000})
    s = ps.summary(rows)
    assert s["pass"] == 3 and s["fail"] == 1 and s["pass_fake"] == 2 and s["pass_real"] == 1
    assert s["delta_7030_pct"] == pytest.approx(1.0) and s["delta_5050_pct"] == pytest.approx(1.7)
    assert s["delta_7030_ex_top1_pct"] == pytest.approx(0.1) and s["delta_7030_ex_top2_pct"] == pytest.approx(-0.2)
    assert s["delta_5050_real_pct"] == pytest.approx(-0.3) and s["delta_5050_fake_pct"] == pytest.approx(2.0)
    assert s["A_worst_day_pct"] == pytest.approx(-2.8) and s["ready_for_review"] is False


# ── worker 실제 손절 경로 ─────────────────────────────────────────────────
def _n1_stop_run(monkeypatch, tmp_path, *, enabled):
    from tests.macd2.test_fixed_exit_15 import _run
    monkeypatch.setattr(config, "P70R_SHADOW_ENABLED", enabled)
    # 직전 3분 저점 9,800 -> 손절 tick 9,860 은 +0.61% 반등(PASS)
    _ticks([9_800.0] + [9_840.0] * 17, end=datetime(2026, 1, 1, tzinfo=KST))   # 다른 시각 -> 영향 없음 확인용 잡음
    return _run(monkeypatch, tmp_path, mode=strategy_mode.MODE_N1, on=False, price=9_860.0, bar_close=9_860.0)


def test_worker_n1_morning_stop_records_shadow_without_changing_the_order(monkeypatch, tmp_path):
    state, orders, result = _n1_stop_run(monkeypatch, tmp_path, enabled=True)
    sells = [o for o in orders if o.side == "SELL"]
    assert len(sells) == 1 and sells[0].executed_qty == 10          # 실제 주문은 100% 전량
    assert f"{config.EXIT_TW_STOP_LOSS}:{SYM}" in result.actions
    rs = ps.rows()
    assert len(rs) == 1
    r = rs[0]
    assert r["exit_reason"] == config.EXIT_TW_STOP_LOSS and r["qty"] == 10 and r["stop_pct"] == pytest.approx(-1.3)
    assert r["P70R_SHADOW"] == ps.UNAVAILABLE                      # 손절 시각 직전 3분 tick 이 없다
    assert r["sell_requested_at"] and r["sell_confirmed_at"] and r["sell_reconciled_at"]
    assert r["stop_decision_at"] == r["decision_at"] and r["stop_bar_end_at"]
    assert r["bar_end_to_decision_sec"] is not None and r["delay_bucket"] != ""


def test_worker_parity_shadow_on_vs_off(monkeypatch, tmp_path):
    def core(rows):
        keep = ("side", "symbol", "executed_qty", "executed_price", "exit_reason", "net_pnl", "position_after")
        return [{k: r.get(k) for k in keep} for r in rows]

    s_on, o_on, r_on = _n1_stop_run(monkeypatch, tmp_path / "on", enabled=True)
    led_on = core(ledger.load_execution_ledger())
    ledger.EXECUTION_LEDGER_PATH.unlink()
    ps.reset_for_tests()
    s_off, o_off, r_off = _n1_stop_run(monkeypatch, tmp_path / "off", enabled=False)
    led_off = core(ledger.load_execution_ledger())
    assert [(o.side, o.symbol, o.executed_qty, o.executed_price) for o in o_on] == \
           [(o.side, o.symbol, o.executed_qty, o.executed_price) for o in o_off]
    assert r_on.actions == r_off.actions and led_on == led_off
    assert s_on.position == s_off.position


def test_worker_n1_morning_stop_pass_when_rebounding_from_3m_low(monkeypatch, tmp_path):
    from tests.macd2.test_early_take_profit_worker import _SESSION_START_NOW as now
    from tests.macd2.test_fixed_exit_15 import _run
    for i in range(18):                                    # now-180s..now-10s, 저점 9,800
        ps.observe(now - timedelta(seconds=180 - 10 * i), {SYM: 9_800.0 if i == 3 else 9_840.0})
    state, orders, result = _run(monkeypatch, tmp_path, mode=strategy_mode.MODE_N1, on=False,
                                 price=9_860.0, bar_close=9_860.0)
    assert [o.executed_qty for o in orders if o.side == "SELL"] == [10]
    r = ps.rows()[0]
    assert r["P70R_SHADOW"] == ps.PASS and r["low3"] == 9_800.0
    assert r["rebound_pct"] == pytest.approx((9_860.0 / 9_800.0 - 1) * 100, abs=1e-3)


def test_worker_b3_governed_position_stop_is_not_recorded(monkeypatch, tmp_path):
    """CHOP 진입 P3(B3 관리 중) 포지션도 완성봉 −1.3% 래더 손절을 탄다 -> 실제 손절은 나가지만 shadow 대상이 아니다."""
    from tests.macd2.test_fixed_exit_15 import _run
    state, orders, result = _run(monkeypatch, tmp_path, mode=strategy_mode.MODE_P3, on=False,
                                 price=9_950.0, bar_close=9_860.0)
    assert [o.executed_qty for o in orders if o.side == "SELL"] == [10]
    assert f"{config.EXIT_TW_STOP_LOSS}:{SYM}" in result.actions
    assert ps.rows() == []


def test_sell_delay_is_recorded_and_bucketed():
    """손절봉 완성(09:15:00) -> 판정 09:15:05 -> SELL 요청 09:16:12 (10/29 실거래 모양)."""
    _ticks([9_850.0] * 18)
    g = ps.gate(SYM, T0, 9_910.0)
    o = _Outcome("D1")
    o.timestamps["sell_requested_at"] = datetime(2026, 10, 29, 9, 16, 12, 500000, tzinfo=KST).isoformat()
    ps.record_stop(now=T0, symbol=SYM, qty=100, entry_price=10_000.0, stop_pct=-1.3, bar_net_pct=-1.4,
                   decision_price=9_910.0, gate_info=g, outcome=o, exit_reason=config.EXIT_TW_STOP_LOSS)
    r = _row()
    assert r["stop_bar_end_at"] == datetime(2026, 10, 29, 9, 15, tzinfo=KST).isoformat()
    assert r["bar_end_to_decision_sec"] == 5.0
    assert r["decision_to_request_ms"] == 67_500.0
    assert r["bar_end_to_request_sec"] == 72.5 and r["delay_bucket"] == ">60s"
    assert [ps.delay_bucket(x) for x in (0.2, 10.0, 25, 59, 61, None)] == \
           ["<=10s", "<=10s", "10-30s", "30-60s", ">60s", "UNKNOWN"]


def test_summary_by_delay_bucket():
    base = {"done": True, "A_pct": -1.4, "B7030_pct": -1.4, "C5050_pct": -1.4, "delta_7030_krw": 0, "delta_5050_krw": 0}
    rows = [dict(base, trading_date="20261029", P70R_SHADOW=ps.PASS, fake_stop=True, delay_bucket=">60s",
                 delta_7030_pct=0.6, delta_5050_pct=1.0),
            dict(base, trading_date="20261029", P70R_SHADOW=ps.FAIL, fake_stop=False, delay_bucket="<=10s",
                 delta_7030_pct=0.0, delta_5050_pct=0.0),
            dict(base, trading_date="20261030", P70R_SHADOW=ps.PASS, fake_stop=False, delay_bucket="<=10s",
                 delta_7030_pct=-0.3, delta_5050_pct=-0.5)]
    s = ps.summary(rows)["by_delay"]
    assert s["<=10s"] == {"stops": 2, "pass": 1, "pass_rate": 0.5, "fake": 0, "A_total_pct": -2.8,
                          "delta_7030_pct": -0.3, "delta_5050_pct": -0.5}
    assert s[">60s"]["pass_rate"] == 1.0 and s[">60s"]["delta_5050_pct"] == 1.0
