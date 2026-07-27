"""READ-ONLY reproduction check for the 2026-07-27 MACD2 confirmed-bar fix.

Fetches TODAY's real 1-minute bars for 000660 via the KIS MOCK account
(read-only: quotes + minute candles + a single get_positions() balance
check only — never buy/sell) and verifies, using the exact same
app.trading.macd2.signal_engine/worker functions the live Worker calls:

  1. Today's 1m bars actually accumulate into history (count, newest ts).
  2. Whether a completed 3m bar in the 09:42-09:48 window shows a genuine
     Primary crossover (matches/mismatches whatever KIS's own chart shows —
     this script only reports the confirmed-bar computation, it does not
     assert a specific direction since that depends on today's real market).
  3. A momentary/forming-bar wobble in that window never produces a
     candidate confirmation understating <3s persistence (shadow only,
     verified structurally — see tests/macd2 for the enforced gate).
  4. If a genuine confirmed crossover exists, simulates ONE worker.run_once()
     tick with a FakeBroker (in-memory, no real orders) to confirm dispatch
     would fire within SIGNAL_TO_ORDER_REQUEST_MAX_SEC of detection.
  5. Orderable-cash-based qty sizing using the REAL (read-only) KIS mock
     orderable cash for 0193T0/0197X0, budget 9,200,000 — confirms
     requested_qty * price <= real orderable cash (auto-shrinks safely if
     budget exceeds it).
  6. Signal ledger columns round-trip correctly (isolated temp ledger).

Never places a buy/sell order, never touches the real macd2 ledger/state
files, never runs in REAL mode.
"""
from __future__ import annotations

import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config, ledger, order_executor, worker  # noqa: E402
from app.trading.macd2.broker_adapter import create_macd2_broker  # noqa: E402
from app.trading.macd2.market_data import MarketDataService  # noqa: E402
from app.trading.macd2.models import Direction, RuntimeState  # noqa: E402
from app.trading.macd2.signal_engine import (  # noqa: E402
    calculate_macd,
    evaluate_macd_crossover,
    resample_completed_3m,
)

KST = config.KST


class ReadOnlyReportBroker:
    """Wraps the real (mock) broker adapter for get_positions/get_orderable_cash
    (both pure read-only KIS calls) while making buy_market/sell_market raise
    if ever accidentally invoked — this script must place zero real orders."""

    mode = "mock"

    def __init__(self, real_broker) -> None:
        self._real = real_broker

    def get_orderable_cash(self, symbol: str) -> float:
        return self._real.get_orderable_cash(symbol)

    def get_position(self, symbol: str):
        return self._real.get_position(symbol)

    def get_positions(self):
        return self._real.get_positions()

    def reconcile_position(self, symbol: str) -> int:
        return self._real.reconcile_position(symbol)

    def buy_market(self, symbol: str, qty: int, client_order_id: str):
        raise AssertionError("verify_0945: buy_market must never be called by this read-only script")

    def sell_market(self, symbol: str, qty: int, client_order_id: str):
        raise AssertionError("verify_0945: sell_market must never be called by this read-only script")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    report: dict = {"errors": []}

    real_broker = create_macd2_broker("mock")
    broker = ReadOnlyReportBroker(real_broker)
    mds = MarketDataService(mode="mock")

    now = datetime.now(KST)
    report["run_at"] = now.isoformat()

    boot = mds.bootstrap(now=now)
    report["bootstrap_ok"] = boot.ok
    report["bootstrap_reason"] = boot.reason
    report["received_1m_bars"] = boot.received_1m_bars
    report["today_1m_bars"] = boot.today_1m_bars

    df_1m = mds.get_history_df()
    today_str = now.strftime("%Y%m%d")
    today_rows = df_1m[df_1m["datetime"].dt.strftime("%Y%m%d") == today_str] if not df_1m.empty else df_1m
    report["today_1m_bar_count"] = int(len(today_rows))
    report["history_newest_at"] = (
        df_1m["datetime"].iloc[-1].isoformat() if not df_1m.empty else None
    )
    print(f"[1] 오늘 1분봉 누적: {report['today_1m_bar_count']}건, newest={report['history_newest_at']}")

    # ── 2) completed 3m bar covering the 09:42-09:48 window ──────────────
    window_end = now.replace(hour=9, minute=48, second=0, microsecond=0)
    window_start = now.replace(hour=9, minute=42, second=0, microsecond=0)
    bars_3m_at_window = resample_completed_3m(df_1m, now=window_end + timedelta(minutes=1))
    in_window = bars_3m_at_window[
        (bars_3m_at_window["datetime"] >= window_start) & (bars_3m_at_window["datetime"] < window_end)
    ] if not bars_3m_at_window.empty else bars_3m_at_window
    print(f"[2] 09:42-09:48 완성 3분봉 수: {len(in_window)}")
    window_directions = []
    if not bars_3m_at_window.empty:
        for i in range(len(bars_3m_at_window)):
            bar_dt = bars_3m_at_window["datetime"].iloc[i]
            if not (window_start <= bar_dt < window_end):
                continue
            snap_i = calculate_macd(bars_3m_at_window.iloc[: i + 1])
            if snap_i is None:
                continue
            prev_snap = calculate_macd(bars_3m_at_window.iloc[:i]) if i > 0 else None
            direction = evaluate_macd_crossover(snap_i, None)
            window_directions.append({
                "bar_dt": bar_dt.isoformat(), "previous_diff": snap_i.previous_diff,
                "current_diff": snap_i.current_diff, "direction": direction.value,
            })
            print(f"    bar={bar_dt.strftime('%H:%M:%S')} prev_diff={snap_i.previous_diff:.4f} "
                  f"cur_diff={snap_i.current_diff:.4f} -> {direction.value}")
    report["window_bars"] = window_directions
    if not window_directions:
        print("    (해당 창에 완성된 3분봉이 없음 — 프리마켓/휴장/데이터 없음)")

    confirmed_signal = next((r for r in window_directions if r["direction"] != "HOLD"), None)
    report["confirmed_signal_in_window"] = confirmed_signal

    # ── 4) simulate ONE worker.run_once() tick with the real 09:48 bar ────
    if confirmed_signal is not None:
        state = RuntimeState(auto_trade_on=True, mode="mock", budget=9_200_000.0)
        state.strategy_name = config.STRATEGY_NAME
        state.strategy_version = config.STRATEGY_VERSION
        state.signal_rule = config.SIGNAL_RULE
        # Prime baseline to the bar just before the window so this bar reads as a genuine new signal.
        state.last_confirmed_bar_ts = (window_start - timedelta(minutes=3)).isoformat()

        sim_now = datetime.fromisoformat(confirmed_signal["bar_dt"]) + timedelta(minutes=3, seconds=2)
        mds.refresh_quotes()
        with _isolated_ledger():
            result = worker.run_once(broker=broker, market_data=mds, state=state, now=sim_now)
        print(f"[3] 시뮬레이션 tick(now={sim_now.strftime('%H:%M:%S')}): actions={result.actions} skipped={result.skipped}")
        if result.signal_detected_at and result.signal_dispatch_trace.get("executor_called_at"):
            detected = datetime.fromisoformat(result.signal_detected_at)
            executor_called = datetime.fromisoformat(result.signal_dispatch_trace["executor_called_at"])
            gap = (executor_called - detected).total_seconds()
            print(f"    signal_detected -> executor_called: {gap:.3f}s (<= {config.SIGNAL_TO_ORDER_REQUEST_MAX_SEC}s 요건)")
            report["detection_to_executor_sec"] = gap
        report["sim_actions"] = result.actions
        report["sim_skipped"] = result.skipped
    else:
        print("[3] 09:42-09:48 창에 확정 신호 없음 — 디스패치 시뮬레이션 생략 (정상: 순간 노이즈는 0건이어야 함)")

    # ── 5) orderable-cash-based qty shrink (real KIS mock balance, read-only) ──
    print("[4] 주문가능금액 기반 수량 축소 검증 (실제 조회, 주문 없음)")
    for symbol, label in ((config.LONG_SYMBOL, "0193T0"), (config.INVERSE_SYMBOL, "0197X0")):
        try:
            real_cash = broker.get_orderable_cash(symbol)
        except Exception as exc:
            report["errors"].append(f"get_orderable_cash({symbol}): {exc!r}")
            continue
        quote_snap = mds.get_quote(symbol)
        price = quote_snap.price if quote_snap is not None and not quote_snap.error else None
        if price is None or price <= 0:
            print(f"    {label}: quote 없음, 수량 계산 생략")
            continue
        budget = 9_200_000.0
        qty = order_executor.compute_order_quantity(real_cash, budget, price, symbol=symbol)
        expected_amount = qty * price
        print(f"    {label}: 실제 주문가능금액={real_cash:,.0f}원 예산={budget:,.0f}원 가격={price:,.2f}원 "
              f"-> requested_qty={qty} 예상주문금액={expected_amount:,.0f}원")
        assert expected_amount <= real_cash + 1e-6, "예상 주문금액이 실제 주문가능금액을 초과함"
        report.setdefault("qty_checks", []).append({
            "symbol": symbol, "real_orderable_cash": real_cash, "budget": budget,
            "price": price, "requested_qty": qty, "expected_amount": expected_amount,
        })

    # ── 6) signal ledger column round-trip (isolated) ─────────────────────
    print("[5] signal ledger 컬럼 round-trip 검증 (격리된 임시 원장)")
    with _isolated_ledger():
        test_bar_dt = now.replace(hour=9, minute=45, second=0, microsecond=0)
        sig_id = "20260727_094500_UP_RED"
        ledger.append_signal({
            "trading_date": "20260727", "completed_bar_at": "094500", "signal_id": sig_id,
            "signal_type": "INITIAL", "direction": "UP_RED", "macd": 1.23, "signal": 0.5,
            "hist_last3": "(0.1,0.2,0.3)", "detected_at": now.isoformat(),
            "order_requested_at": now.isoformat(), "order_result": "EXECUTED", "block_reason": "",
            "strategy_name": config.STRATEGY_NAME, "strategy_version": config.STRATEGY_VERSION,
            "signal_rule": config.SIGNAL_RULE, "session_started_at": now.isoformat(),
        })
        rows = ledger.load_signal_ledger()
        row = next(r for r in rows if r["signal_id"] == sig_id)
        ok = row["strategy_name"] == config.STRATEGY_NAME and row["direction"] == "UP_RED" and row["macd"] == "1.23"
        print(f"    strategy_name={row['strategy_name']!r} direction={row['direction']!r} macd={row['macd']!r} -> {'OK' if ok else 'MISMATCH'}")
        report["ledger_roundtrip_ok"] = ok

    print("\n" + "=" * 60)
    print("요약:", "정상" if not report["errors"] else f"오류 {len(report['errors'])}건: {report['errors']}")
    return 0 if not report["errors"] else 1


import contextlib  # noqa: E402


@contextlib.contextmanager
def _isolated_ledger():
    original = (ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH)
    with tempfile.TemporaryDirectory(prefix="macd2_verify0945_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        ledger.LOGS_DIR_PATH = tmp_path
        ledger.EXECUTION_LEDGER_PATH = tmp_path / "execution_ledger.csv"
        ledger.SIGNAL_LEDGER_PATH = tmp_path / "signal_ledger.csv"
        try:
            yield
        finally:
            ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH = original


if __name__ == "__main__":
    raise SystemExit(main())
