"""MACD2 pre-market -> "market open assumed" dry-run simulator.

Purpose (2026-07-27 사전장 검증): 실제 KIS 시세조회는 별도로
scripts/macd2_read_only_kis_smoke.py 로 확인(정상, mock 계좌, read-only).
정규장이 아직 열리지 않았으므로 "오늘"의 실시간 1분봉은 존재하지 않는다 —
대신 가장 최근으로 확보돼 있는 완전한 3종목(000660/0193T0/0197X0) 실거래일
1분봉(2026-07-23, data/cache/naver_multi_1m/*.csv)을 "장이 열렸다"고 가정한
리플레이 테이프로 삼아, 라이브 워커가 실제로 매 틱 호출하는
``app.trading.macd2.worker.run_once`` 함수를 그대로 분 단위로 반복 호출한다
(신호 계산/체결/손절익절 로직 재구현 없음 — 운영 코드와 동일 경로).

검증 대상 3가지:
  1) MACD forming-crossover 깃발이 정상적으로 형성되는가
  2) 깃발이 형성된 바로 그 틱에 매수/매도가 함께 실행되는가 (동시성)
  3) 매수 후 보유 중 손절(-1.5%)/익절 락(+1.5% 활성화, 0.8%p 반납 시 청산)
     기준을 매 틱 감시하다가 조건 충족 시 정확히 매도되는가

네트워크 호출 없음(순수 오프라인 리플레이). 원장은 임시 디렉터리로 격리되어
실제 macd2_signal_ledger.csv / macd2_execution_ledger.csv 에는 어떤 것도
기록되지 않는다.
"""
from __future__ import annotations

import contextlib
import json
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.macd2 import config, ledger, worker  # noqa: E402
from app.trading.macd2.broker_adapter import BrokerOrderResult  # noqa: E402
from app.trading.macd2.models import Direction, PositionSnapshot, QuoteSnapshot, RuntimeState  # noqa: E402

KST = config.KST
CACHE_DIR = ROOT / "data" / "cache" / "naver_multi_1m"
SIM_TRADING_DATE = "20260723"  # 가장 최근으로 확보된 완전한 3종목 실거래일 (장 개설 가정용)
INITIAL_BUDGET = 10_000_000.0
ADVERSE_SLIPPAGE_PCT = 0.05


class FakeBroker:
    """오프라인 체결 시뮬레이터. 손익/체결가 계산은 order_executor가 그대로 사용."""

    mode = "mock"

    def __init__(self, cash: float) -> None:
        self.cash = float(cash)
        self.position: Optional[PositionSnapshot] = None
        self.fill_prices: dict[str, float] = {}
        self._order_seq = 0
        self.sequence: list[str] = []

    def set_fill_price(self, symbol: str, price: float) -> None:
        self.fill_prices[symbol] = float(price)

    def get_orderable_cash(self, symbol: str) -> float:
        del symbol
        return self.cash

    def get_position(self, symbol: str) -> Optional[PositionSnapshot]:
        if self.position and self.position.symbol == symbol:
            return self.position
        return None

    def get_positions(self) -> list[PositionSnapshot]:
        return [self.position] if self.position else []

    def reconcile_position(self, symbol: str) -> int:
        return int(self.position.quantity) if self.position and self.position.symbol == symbol else 0

    def _next_order_id(self) -> str:
        self._order_seq += 1
        return f"SIM-{self._order_seq:06d}"

    def buy_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self.fill_prices.get(symbol)
        self.sequence.append(f"BUY_REQUEST:{symbol}")
        if price is None or qty < 1:
            return BrokerOrderResult(False, self._next_order_id(), symbol, "BUY", qty, 0, 0.0, "NO_FILL_PRICE")
        fill_price = price * (1 + ADVERSE_SLIPPAGE_PCT / 100.0)
        self.cash -= fill_price * qty
        self.position = PositionSnapshot(symbol=symbol, quantity=qty, avg_price=fill_price, entry_at=datetime.now(KST))
        self.sequence.append(f"BUY_FILLED:{symbol}")
        return BrokerOrderResult(True, self._next_order_id(), symbol, "BUY", qty, qty, fill_price, "OK")

    def sell_market(self, symbol: str, qty: int, client_order_id: str) -> BrokerOrderResult:
        del client_order_id
        price = self.fill_prices.get(symbol)
        self.sequence.append(f"SELL_REQUEST:{symbol}")
        if price is None or self.position is None or self.position.symbol != symbol or self.position.quantity < qty:
            return BrokerOrderResult(False, self._next_order_id(), symbol, "SELL", qty, 0, 0.0, "NO_POSITION")
        fill_price = price * (1 - ADVERSE_SLIPPAGE_PCT / 100.0)
        self.cash += fill_price * qty
        self.position = None
        self.sequence.append(f"SELL_FILLED:{symbol}")
        return BrokerOrderResult(True, self._next_order_id(), symbol, "SELL", qty, qty, fill_price, "OK")


class FakeMarketData:
    """MarketDataService와 동일한 공개 인터페이스만 제공하는 오프라인 대역.

    worker.run_once()가 실제로 호출하는 메서드만 구현한다:
    get_history_df / get_quote / quote_statuses / quote_normalization_diag.
    """

    def __init__(self) -> None:
        self._df_1m = pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
        self._quotes: dict[str, QuoteSnapshot] = {}

    def set_history(self, df: pd.DataFrame) -> None:
        self._df_1m = df

    def get_history_df(self) -> pd.DataFrame:
        return self._df_1m.copy()

    def set_quote(self, symbol: str, price: float, now: datetime) -> None:
        self._quotes[symbol] = QuoteSnapshot(symbol=symbol, price=float(price), fetched_at=now, age_sec=0.0, source="sim", error=None)

    def get_quote(self, symbol: str) -> Optional[QuoteSnapshot]:
        return self._quotes.get(symbol)

    def quote_statuses(self, symbols=(config.WATCH_SYMBOL, config.LONG_SYMBOL, config.INVERSE_SYMBOL)) -> dict[str, str]:
        out = {}
        for s in symbols:
            snap = self._quotes.get(s)
            if snap is None or snap.error or snap.price <= 0:
                out[s] = "MISSING"
            else:
                out[s] = "VALID"
        return out

    def quote_normalization_diag(self) -> dict[str, Any]:
        return {}


@contextlib.contextmanager
def _isolated_ledger():
    """replay_macd2.py와 동일한 방식: 실제 macd2 원장 CSV를 절대 건드리지 않도록
    ledger.py의 경로 상수를 임시 디렉터리로 바꿔치기했다가 종료 시 복원한다."""
    original = (ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH)
    with tempfile.TemporaryDirectory(prefix="macd2_dryrun_") as tmp_dir:
        tmp_path = Path(tmp_dir)
        ledger.LOGS_DIR_PATH = tmp_path
        ledger.EXECUTION_LEDGER_PATH = tmp_path / "sim_execution_ledger.csv"
        ledger.SIGNAL_LEDGER_PATH = tmp_path / "sim_signal_ledger.csv"
        try:
            yield
        finally:
            ledger.LOGS_DIR_PATH, ledger.EXECUTION_LEDGER_PATH, ledger.SIGNAL_LEDGER_PATH = original


def _load_symbol_frame(symbol: str) -> pd.DataFrame:
    df = pd.read_csv(CACHE_DIR / f"{symbol}_1m.csv")
    df["datetime"] = pd.to_datetime(df["datetime"], errors="coerce")
    df = df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
    df["datetime"] = df["datetime"].dt.tz_localize(KST)
    return df


def _summarize_from_rows(execution_rows: list[dict[str, Any]], *, budget: float) -> dict[str, Any]:
    buy_rows = [r for r in execution_rows if str(r.get("side") or "").upper() == "BUY"]
    sell_rows = [r for r in execution_rows if str(r.get("side") or "").upper() == "SELL"]
    net_values = [float(r.get("net_pnl") or 0.0) for r in sell_rows]
    gross_pnl = sum(float(r.get("gross_pnl") or 0.0) for r in execution_rows)
    total_cost = sum(float(r.get("fee") or 0.0) for r in execution_rows)
    net_pnl = sum(net_values)
    wins = [v for v in net_values if v > 0]
    losses = [v for v in net_values if v < 0]
    win_rate = (len(wins) / len(net_values) * 100.0) if net_values else 0.0
    profit_factor = (sum(wins) / abs(sum(losses))) if losses else (None if not wins else float("inf"))
    equity = peak = max_dd = 0.0
    for v in net_values:
        equity += v
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)
    return {
        "buy_count": len(buy_rows), "sell_count": len(sell_rows), "round_trip_count": len(sell_rows),
        "gross_pnl": round(gross_pnl, 2), "total_cost": round(total_cost, 2), "net_pnl": round(net_pnl, 2),
        "return_pct": round((net_pnl / budget * 100.0) if budget else 0.0, 4),
        "win_rate_pct": round(win_rate, 2),
        "profit_factor": round(profit_factor, 4) if isinstance(profit_factor, float) and profit_factor != float("inf") else profit_factor,
        "max_drawdown": round(max_dd, 2),
    }


def run_simulation() -> dict[str, Any]:
    watch_full = _load_symbol_frame(config.WATCH_SYMBOL)
    long_full = _load_symbol_frame(config.LONG_SYMBOL)
    inv_full = _load_symbol_frame(config.INVERSE_SYMBOL)

    is_sim_day = watch_full["datetime"].dt.strftime("%Y%m%d") == SIM_TRADING_DATE
    prior_history = watch_full[~is_sim_day].reset_index(drop=True)  # 07-15 ~ 07-22 warmup
    sim_rows = watch_full[is_sim_day].reset_index(drop=True)
    long_by_ts = long_full.set_index("datetime")["close"]
    inv_by_ts = inv_full.set_index("datetime")["close"]

    broker = FakeBroker(INITIAL_BUDGET)
    market_data = FakeMarketData()
    market_data.set_history(prior_history)

    state = RuntimeState(auto_trade_on=True, mode="mock", budget=INITIAL_BUDGET)
    session_start = sim_rows["datetime"].iloc[0].to_pydatetime()
    worker.initialize_strategy_session(state, market_data, now=session_start)

    flag_events: list[dict[str, Any]] = []
    tick_log: list[dict[str, Any]] = []
    accum = prior_history.copy()

    with _isolated_ledger():
        for i in range(len(sim_rows)):
            row = sim_rows.iloc[i]
            now = row["datetime"].to_pydatetime()
            accum = pd.concat([accum, sim_rows.iloc[[i]]], ignore_index=True)
            market_data.set_history(accum)

            watch_price = float(row["close"])
            long_price = float(long_by_ts.get(row["datetime"], long_by_ts.iloc[max(0, min(i, len(long_by_ts) - 1))]))
            inv_price = float(inv_by_ts.get(row["datetime"], inv_by_ts.iloc[max(0, min(i, len(inv_by_ts) - 1))]))

            market_data.set_quote(config.WATCH_SYMBOL, watch_price, now)
            market_data.set_quote(config.LONG_SYMBOL, long_price, now)
            market_data.set_quote(config.INVERSE_SYMBOL, inv_price, now)
            broker.set_fill_price(config.LONG_SYMBOL, long_price)
            broker.set_fill_price(config.INVERSE_SYMBOL, inv_price)

            prev_flag = state.provisional_flag
            prev_position = state.position

            result = worker.run_once(broker=broker, market_data=market_data, state=state, now=now)

            new_flag = state.provisional_flag
            if new_flag is not None and (prev_flag != new_flag or i == 0):
                flag_events.append({
                    "time": now.strftime("%H:%M:%S"),
                    "direction": new_flag.value if hasattr(new_flag, "value") else str(new_flag),
                    "actions_same_tick": list(result.actions),
                    "watch_price": watch_price,
                })

            if result.actions or (prev_position is None) != (state.position is None) or (
                prev_position is not None and state.position is not None and prev_position.symbol != state.position.symbol
            ):
                tick_log.append({
                    "time": now.strftime("%H:%M:%S"),
                    "actions": list(result.actions),
                    "provisional_flag": new_flag.value if new_flag is not None else None,
                    "position_before": (prev_position.symbol, prev_position.quantity) if prev_position else None,
                    "position_after": (state.position.symbol, state.position.quantity, round(state.position.avg_price, 2)) if state.position else None,
                    "peak_net_return": round(state.peak_net_return, 4),
                    "profit_lock_active": state.profit_lock_active,
                    "block_reason": state.order_block_reason,
                })

        execution_rows = ledger.load_execution_ledger(limit=1000)
        signal_rows = ledger.load_signal_ledger(limit=2000)
        # NOTE: order_executor timestamps every leg with the REAL wall clock
        # (datetime.now(KST)), never the simulated ``now`` passed into
        # worker.run_once — so ledger.summarize_daily_trading's own
        # trading_date-prefix filter (matched against real timestamps) would
        # find zero rows here. Compute the same stats directly from
        # execution_rows instead of relying on that date filter.
        summary = _summarize_from_rows(execution_rows, budget=INITIAL_BUDGET)

    return {
        "sim_trading_date": SIM_TRADING_DATE,
        "flag_events": flag_events,
        "tick_log": tick_log,
        "execution_rows": execution_rows,
        "signal_rows_count": len(signal_rows),
        "summary": summary,
        "final_cash": broker.cash,
        "final_position": (broker.position.symbol, broker.position.quantity) if broker.position else None,
        "broker_sequence": broker.sequence,
    }


def _print_report(out: dict[str, Any]) -> None:
    print("=" * 78)
    print(f"MACD2 dry-run — assumed market-open replay on {out['sim_trading_date']}")
    print("(prior-day warmup: 07-15~07-22 from data/cache/naver_multi_1m; offline, no network)")
    print("=" * 78)

    print("\n[1] MACD forming-crossover 깃발 형성 타임라인")
    if not out["flag_events"]:
        print("  깃발 형성 없음 (HOLD만 지속)")
    for ev in out["flag_events"]:
        same_tick = "YES" if ev["actions_same_tick"] else "no"
        print(f"  {ev['time']}  flag={ev['direction']:<10} watch_price={ev['watch_price']:.0f}  "
              f"same-tick order actions={ev['actions_same_tick'] or '(none this tick)'}  fired={same_tick}")

    print("\n[2] 매수/매도/청산 이벤트 로그 (포지션 변화 또는 액션이 있었던 틱만)")
    for t in out["tick_log"]:
        print(f"  {t['time']}  actions={t['actions']}  flag={t['provisional_flag']}  "
              f"before={t['position_before']}  after={t['position_after']}  "
              f"peak_ret={t['peak_net_return']}%  profit_lock_active={t['profit_lock_active']}  "
              f"block={t['block_reason']}")

    print("\n[3] 체결 원장 (BUY/SELL 레그별)")
    for r in out["execution_rows"]:
        print(f"  {r['timestamp']}  {r['side']:<4} {r['symbol']}  qty={r['executed_qty']}  "
              f"price={float(r['executed_price']):.2f}  net_pnl={r['net_pnl']}  exit_reason={r['exit_reason']}")

    s = out["summary"]
    print("\n[4] 일일 집계")
    print(f"  buy_count={s['buy_count']}  sell_count={s['sell_count']}  round_trips={s['round_trip_count']}")
    print(f"  gross_pnl={s['gross_pnl']:,.0f}  total_cost={s['total_cost']:,.0f}  net_pnl={s['net_pnl']:,.0f}")
    print(f"  win_rate={s['win_rate_pct']}%  profit_factor={s['profit_factor']}  max_drawdown={s['max_drawdown']:,.0f}")
    print(f"  final_cash={out['final_cash']:,.0f}  final_position={out['final_position']}")

    exit_reasons = [r["exit_reason"] for r in out["execution_rows"] if r["side"] == "SELL"]
    print(f"\n[5] 청산 사유 분포: {dict((r, exit_reasons.count(r)) for r in set(exit_reasons))}")


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    out = run_simulation()
    _print_report(out)
    out_path = ROOT / "data" / "state" / "macd2_dryrun_market_open_report.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"\nWrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
