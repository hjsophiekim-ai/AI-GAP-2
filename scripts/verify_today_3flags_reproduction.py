"""2026-07-27 today-session reproduction: verify MACD2's confirmed-bar
Primary path (worker.run_once(), no reimplemented signal logic) catches the
3 real KIS flags reported by the user —

    1) 09:45 UP_RED
    2) 10:09 DOWN_BLUE
    3) 10:42 UP_RED  (also reported live as "10:45 레드 up", same bar —
       10:42 is this bar's start/label time, 10:45 is its completion/close
       time; both refer to the same completed 3m bar)

— and that each connects to an order via the real Worker order path.

Two passes, both against the SAME real 1-minute KIS data (mock account,
fetched once, read-only for the fetch itself):

  --mode=sim  (default): worker.run_once() + in-memory FakeBroker — zero
    external side effects, safe to re-run any number of times.
  --mode=real: worker.run_once() connected to the REAL KIS MOCK (paper
    trading) broker adapter — this DOES place real orders on the mock
    account (no real money; this is the sanctioned mock auto-trading path
    this module exists to run). Never REAL mode.

Both passes replay bar-by-bar from market open (09:00) through "now",
calling worker.run_once() once per new completed 3m bar exactly like the
live Worker would — no separate/reimplemented crossover math for dispatch.
calculate_macd() (the same shared function run_once() uses internally) is
also called here, read-only, purely to print the comparison table.
"""
from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config, ledger, worker  # noqa: E402
from app.trading.macd2.broker_adapter import BrokerOrderResult, create_macd2_broker  # noqa: E402
from app.trading.macd2.market_data import MarketDataService  # noqa: E402
from app.trading.macd2.models import PositionSnapshot, QuoteSnapshot, RuntimeState  # noqa: E402
from app.trading.macd2.signal_engine import calculate_macd, resample_completed_3m  # noqa: E402

KST = config.KST
EXPECTED_FLAGS = [
    ("09:45", "UP_RED"),
    ("10:09", "DOWN_BLUE"),
    ("10:42", "UP_RED"),
]


class FakeBroker:
    mode = "mock"

    def __init__(self, cash: float) -> None:
        self.cash = cash
        self.position = None
        self.fill_prices: dict[str, float] = {}
        self._seq = 0
        self.orders: list[dict] = []

    def set_fill_price(self, symbol: str, price: float) -> None:
        self.fill_prices[symbol] = price

    def get_orderable_cash(self, symbol: str) -> float:
        del symbol
        return self.cash

    def get_position(self, symbol: str):
        return self.position if self.position and self.position.symbol == symbol else None

    def get_positions(self):
        return [self.position] if self.position else []

    def reconcile_position(self, symbol: str) -> int:
        return int(self.position.quantity) if self.position and self.position.symbol == symbol else 0

    def _oid(self) -> str:
        self._seq += 1
        return f"SIM-{self._seq:04d}"

    def buy_market(self, symbol, qty, client_order_id):
        del client_order_id
        price = self.fill_prices.get(symbol)
        if price is None or qty < 1:
            return BrokerOrderResult(False, self._oid(), symbol, "BUY", qty, 0, 0.0, "NO_FILL_PRICE")
        self.cash -= price * qty
        self.position = PositionSnapshot(symbol=symbol, quantity=qty, avg_price=price)
        self.orders.append({"side": "BUY", "symbol": symbol, "qty": qty, "price": price})
        return BrokerOrderResult(True, self._oid(), symbol, "BUY", qty, qty, price, "OK")

    def sell_market(self, symbol, qty, client_order_id):
        del client_order_id
        price = self.fill_prices.get(symbol)
        if price is None or self.position is None or self.position.quantity < qty:
            return BrokerOrderResult(False, self._oid(), symbol, "SELL", qty, 0, 0.0, "NO_POSITION")
        self.cash += price * qty
        self.position = None
        self.orders.append({"side": "SELL", "symbol": symbol, "qty": qty, "price": price})
        return BrokerOrderResult(True, self._oid(), symbol, "SELL", qty, qty, price, "OK")


class RealMockBrokerBridge:
    mode = "mock"

    def __init__(self, real_adapter) -> None:
        self._real = real_adapter

    def get_orderable_cash(self, symbol: str) -> float:
        return self._real.get_orderable_cash(symbol)

    def get_position(self, symbol: str):
        return self._real.get_position(symbol)

    def get_positions(self):
        return self._real.get_positions()

    def reconcile_position(self, symbol: str) -> int:
        return self._real.reconcile_position(symbol)

    def buy_market(self, symbol, qty, client_order_id):
        return self._real.buy_market(symbol, qty, client_order_id)

    def sell_market(self, symbol, qty, client_order_id):
        return self._real.sell_market(symbol, qty, client_order_id)


@contextlib.contextmanager
def _isolated_ledger():
    original = (ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH)
    with tempfile.TemporaryDirectory(prefix="macd2_today3flags_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        ledger.LOGS_DIR_PATH = tmp_path
        ledger.EXECUTION_LEDGER_PATH = tmp_path / "execution_ledger.csv"
        ledger.SIGNAL_LEDGER_PATH = tmp_path / "signal_ledger.csv"
        try:
            yield
        finally:
            ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH = original


def _fresh_state() -> RuntimeState:
    state = RuntimeState(auto_trade_on=True, mode="mock", budget=9_200_000.0)
    state.strategy_name = config.STRATEGY_NAME
    state.strategy_version = config.STRATEGY_VERSION
    state.signal_rule = config.SIGNAL_RULE
    return state


def _bootstrap_with_retry(mds: MarketDataService, now: datetime, max_attempts: int = 4):
    for attempt in range(max_attempts):
        boot = mds.bootstrap(now=now)
        df = mds.get_history_df()
        today_ymd = now.strftime("%Y%m%d")
        today_rows = df[df["datetime"].dt.strftime("%Y%m%d") == today_ymd]
        if not today_rows.empty and today_rows["datetime"].iloc[0].strftime("%H:%M") == "09:00":
            return boot, df
        time.sleep(3)
    return boot, mds.get_history_df()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=["sim", "real"], default="sim")
    args = parser.parse_args()

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass

    print(f"=== MACD2 오늘(2026-07-27) 3개 플래그 재현 검증 — mode={args.mode} ===")

    mds = MarketDataService(mode="mock")
    now = datetime.now(KST)
    boot, df_1m = _bootstrap_with_retry(mds, now)
    today_str = now.strftime("%Y%m%d")
    today_rows = df_1m[df_1m["datetime"].dt.strftime("%Y%m%d") == today_str]
    prior_days = sorted(df_1m[df_1m["datetime"].dt.strftime("%Y%m%d") != today_str]["datetime"].dt.strftime("%Y%m%d").unique())
    print(f"bootstrap ok={boot.ok} reason={boot.reason} warm-up prior_days={prior_days}")
    print(f"오늘 1분봉: {len(today_rows)}건, {today_rows['datetime'].iloc[0].strftime('%H:%M')}~{today_rows['datetime'].iloc[-1].strftime('%H:%M')}")

    session_open = now.replace(hour=9, minute=0, second=0, microsecond=0)
    bars_3m_full = resample_completed_3m(df_1m, now=now)
    today_bars = bars_3m_full[bars_3m_full["datetime"] >= session_open].reset_index(drop=True)

    # ── replay bar-by-bar through the REAL worker.run_once() path ───────
    if args.mode == "sim":
        broker = FakeBroker(cash=9_200_000.0)
    else:
        real_adapter = create_macd2_broker("mock")
        broker = RealMockBrokerBridge(real_adapter)
        print("\n*** REAL MOCK 계좌 연결 — 실제 KIS 모의투자 계좌에 주문이 나갑니다 (실제 자금 아님) ***")

    state = _fresh_state()
    trade_log = []
    all_dispatched = []

    with _isolated_ledger():
        for i in range(len(today_bars)):
            bar_dt = today_bars["datetime"].iloc[i].to_pydatetime()
            tick_now = bar_dt + timedelta(minutes=3, seconds=2)
            if tick_now > now:
                break
            watch_row = today_rows[today_rows["datetime"] == bar_dt]
            watch_close = float(watch_row["close"].iloc[0]) if not watch_row.empty else None

            mds_quote_long = mds.get_quote(config.LONG_SYMBOL)
            mds_quote_inv = mds.get_quote(config.INVERSE_SYMBOL)
            long_price = mds_quote_long.price if mds_quote_long and not mds_quote_long.error else 15_000.0
            inv_price = mds_quote_inv.price if mds_quote_inv and not mds_quote_inv.error else 10_000.0

            if args.mode == "sim":
                broker.set_fill_price(config.LONG_SYMBOL, long_price)
                broker.set_fill_price(config.INVERSE_SYMBOL, inv_price)

            df_upto_bar = df_1m[df_1m["datetime"] <= bar_dt + timedelta(minutes=3)]

            class _HistSnapMarketData:
                def get_history_df(self_inner):
                    return df_upto_bar

                def get_quote(self_inner, symbol):
                    if symbol == config.WATCH_SYMBOL:
                        return QuoteSnapshot(symbol, watch_close or 0.0, tick_now, 0.0, "hist", None) if watch_close else None
                    if symbol == config.LONG_SYMBOL:
                        return QuoteSnapshot(symbol, long_price, tick_now, 0.0, "hist", None)
                    if symbol == config.INVERSE_SYMBOL:
                        return QuoteSnapshot(symbol, inv_price, tick_now, 0.0, "hist", None)
                    return None

                def quote_statuses(self_inner, symbols=None):
                    syms = symbols or (config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL)
                    return {s: "VALID" for s in syms}

                def quote_normalization_diag(self_inner):
                    return {}

            prev_position = state.position.symbol if state.position else None
            result = worker.run_once(broker=broker, market_data=_HistSnapMarketData(), state=state, now=tick_now)
            new_position = state.position.symbol if state.position else None
            hhmm = bar_dt.strftime("%H:%M")

            if result.actions:
                trade_log.append({"time": hhmm, "actions": result.actions, "before": prev_position, "after": new_position})

            if args.mode == "real" and result.actions:
                time.sleep(1.0)

        # ground truth of every confirmed-bar evaluation that produced a real
        # (non-HOLD) direction: the signal ledger rows written by
        # worker._dispatch_confirmed_signal / _record_confirmed_blocked_signal
        ledger_rows = ledger.load_signal_ledger(limit=10_000)

    for row in ledger_rows:
        direction = row.get("confirmed_direction") or row.get("direction")
        if not direction or direction == "HOLD":
            continue
        bar_at = row.get("completed_bar_at") or row.get("signal_bar_at") or ""
        try:
            hhmm = datetime.fromisoformat(bar_at).astimezone(KST).strftime("%H:%M")
        except ValueError:
            hhmm = bar_at
        bar_dt_lookup = today_bars[today_bars["datetime"].dt.strftime("%H:%M") == hhmm]
        three_1m = pd.DataFrame()
        if not bar_dt_lookup.empty:
            bdt = bar_dt_lookup["datetime"].iloc[0]
            three_1m = today_rows[(today_rows["datetime"] >= bdt) & (today_rows["datetime"] < bdt + timedelta(minutes=3))]
        all_dispatched.append({
            "time": hhmm,
            "direction": direction,
            "prev_macd": row.get("previous_macd"),
            "prev_signal": row.get("previous_signal"),
            "prev_diff": row.get("previous_diff"),
            "cur_macd": row.get("confirmed_macd"),
            "cur_signal": row.get("confirmed_signal"),
            "cur_diff": row.get("confirmed_diff"),
            "one_min_bars": [(r["datetime"].strftime("%H:%M"), r["close"]) for _, r in three_1m.iterrows()],
            "order_result": row.get("order_result"),
            "block_reason": row.get("block_reason"),
        })

    print(f"\n[프로그램이 실제로 만든 confirmed 신호 목록 — signal ledger 기준] {len(all_dispatched)}건 (Worker completed-bar Primary 경로, worker.run_once() 직접 호출)")
    for f in all_dispatched:
        print(f"  {f['time']} {f['direction']}  prev(macd={f['prev_macd']}, signal={f['prev_signal']}, diff={f['prev_diff']})"
              f"  cur(macd={f['cur_macd']}, signal={f['cur_signal']}, diff={f['cur_diff']})  1m={f['one_min_bars']}"
              f"  order_result={f['order_result']} block_reason={f['block_reason']}")

    print(f"\n[정답 대조표]")
    print(f"{'KIS 정답 시각/방향':<22}{'프로그램 시각/방향':<22}{'일치':<6}")
    matched = 0
    program_times = {f["time"]: f["direction"] for f in all_dispatched}
    for exp_time, exp_dir in EXPECTED_FLAGS:
        got_dir = program_times.get(exp_time)
        ok = got_dir == exp_dir
        matched += int(ok)
        print(f"{exp_time+' '+exp_dir:<22}{(exp_time+' '+got_dir) if got_dir else '없음':<22}{'O' if ok else 'X':<6}")
    extra = [f for f in all_dispatched if f["time"] not in dict(EXPECTED_FLAGS)]
    print(f"일치: {matched}/3, 누락: {3-matched}건, 추가신호: {len(extra)}건 {[e['time']+' '+e['direction'] for e in extra]}")

    print(f"\n[{'SIM' if args.mode=='sim' else 'REAL MOCK'} 주문 디스패치 로그] 총 {len(trade_log)}건")
    for t in trade_log:
        print(f"  {t['time']} {t['actions']} position: {t['before']}->{t['after']}")

    if args.mode == "real":
        print("\n최종 실제 계좌 상태 재조회:")
        try:
            positions = broker.get_positions()
            print("  positions:", [(p.symbol, p.quantity, p.avg_price) for p in positions])
        except Exception as exc:
            print("  조회 실패:", exc)

    return 0 if matched == 3 and not extra else 1


if __name__ == "__main__":
    raise SystemExit(main())
