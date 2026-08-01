from __future__ import annotations

import json
import sys
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import config, ledger, market_data as market_data_module, state_store
from app.trading.tsla_auto.cost_engine import OverseasTradeCostEngine
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.models import Direction, QuoteSnapshot
from app.trading.tsla_auto.worker import initialize_strategy_session, run_once
from scripts.tsla_auto_replay_historical import CACHE_DIR, _isolated_paths, _load_history, _price_at
from tests.tsla_auto.fake_broker import FakeBroker

ET = config.ET
KST = config.KST
TARGET_DAY = date(2026, 7, 31)
GOLDEN_FLAGS = [
    ("10:27", Direction.DOWN_BLUE),
    ("11:45", Direction.UP_RED),
    ("15:24", Direction.DOWN_BLUE),
    ("16:09", Direction.UP_RED),
    ("16:27", Direction.DOWN_BLUE),
    ("16:57", Direction.UP_RED),
]


@dataclass
class ReplayTrade:
    trade_no: int
    entry_reason: str
    entry_time_et: str
    entry_time_kst: str
    candidate_flag: str
    confirmed_flag: str
    order_etf: str
    buy_price: float
    quantity: int
    exit_reason: str = ""
    exit_time_et: str = ""
    exit_time_kst: str = ""
    sell_price: float = 0.0
    pnl_usd: float = 0.0
    pnl_krw: float = 0.0
    cumulative_pnl_usd: float = 0.0
    cumulative_pnl_krw: float = 0.0
    cumulative_return_pct: float = 0.0


def _load_usdkrw() -> float:
    path = ROOT / "data" / "cache" / "global_quotes.json"
    body = json.loads(path.read_text(encoding="utf-8"))
    rate = float((body.get("USDKRW") or {}).get("price") or 0.0)
    if rate <= 0:
        raise RuntimeError("USDKRW cache is missing; TSLA_AUTO has no built-in KRW budget conversion")
    return rate


def _fmt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S %Z")


def _flag_label(direction: Direction) -> str:
    if direction == Direction.UP_RED:
        return "UP"
    if direction == Direction.DOWN_BLUE:
        return "DOWN"
    return "HOLD"


def _golden_trade_list() -> list[dict]:
    rows = []
    for clock, direction in GOLDEN_FLAGS:
        hh, mm = [int(x) for x in clock.split(":")]
        display_at = datetime(TARGET_DAY.year, TARGET_DAY.month, TARGET_DAY.day, hh, mm, tzinfo=ET)
        order_at = display_at + timedelta(minutes=3)
        market_allowed = order_at.time() < config.REGULAR_CLOSE
        rows.append({
            "display_flag_et": _fmt(display_at),
            "order_due_et": _fmt(order_at),
            "flag": _flag_label(direction),
            "order_etf": config.LONG_SYMBOL if direction == Direction.UP_RED else config.INVERSE_SYMBOL,
            "regular_session_order_allowed": market_allowed,
        })
    return rows


def _set_quotes(svc: MarketDataService, broker: FakeBroker, now: datetime, tsla: pd.DataFrame, tsll: pd.DataFrame, tslz: pd.DataFrame) -> dict[str, float]:
    prices = {
        config.SIGNAL_SYMBOL: _price_at(tsla, now),
        config.LONG_SYMBOL: _price_at(tsll, now),
        config.INVERSE_SYMBOL: _price_at(tslz, now),
    }
    for symbol, price in prices.items():
        svc._quotes[symbol] = QuoteSnapshot(symbol, price, datetime.now(ET), 0.0, f"replay_{now.isoformat()}")
        if symbol in config.TRADE_SYMBOLS:
            broker.set_quote(symbol, price)
    return prices


def _close_trade(
    trade: ReplayTrade,
    *,
    now: datetime,
    reason: str,
    sell_price: float,
    usdkrw: float,
    budget_usd: float,
    cumulative_pnl_usd: float,
) -> float:
    pnl = OverseasTradeCostEngine().compute_net_pnl_usd(trade.buy_price, sell_price, trade.quantity)
    net = float(pnl["net_pnl_usd"])
    cumulative = cumulative_pnl_usd + net
    trade.exit_reason = reason
    trade.exit_time_et = _fmt(now.astimezone(ET))
    trade.exit_time_kst = _fmt(now.astimezone(KST))
    trade.sell_price = round(float(sell_price), 4)
    trade.pnl_usd = round(net, 4)
    trade.pnl_krw = round(net * usdkrw, 2)
    trade.cumulative_pnl_usd = round(cumulative, 4)
    trade.cumulative_pnl_krw = round(cumulative * usdkrw, 2)
    trade.cumulative_return_pct = round((cumulative / budget_usd) * 100.0, 4) if budget_usd else 0.0
    return cumulative


def main() -> int:
    usdkrw = _load_usdkrw()
    budget_krw = 9_500_000.0
    budget_usd = round(budget_krw / usdkrw, 2)
    tsla, tsll, tslz = _load_history(TARGET_DAY)

    with _isolated_paths():
        svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (tsla, {}), fetch_quote=lambda mode, symbol: (None, None))
        start = datetime.combine(TARGET_DAY, config.SESSION_OPEN, tzinfo=ET)
        end = datetime.combine(TARGET_DAY, config.REGULAR_CLOSE, tzinfo=ET)
        svc.bootstrap(now=start)
        state = state_store.default_state()
        state.auto_trade_on = True
        state.budget_usd = budget_usd
        state.strategy_name = config.STRATEGY_NAME
        state.strategy_version = config.STRATEGY_VERSION
        state.signal_rule = config.SIGNAL_RULE
        broker = FakeBroker(cash_usd=budget_usd, quotes={
            config.LONG_SYMBOL: _price_at(tsll, start),
            config.INVERSE_SYMBOL: _price_at(tslz, start),
        })
        initialize_strategy_session(state, svc, now=start, worker_instance_id="golden-20260731-replay")

        trades: list[ReplayTrade] = []
        open_trade: ReplayTrade | None = None
        cumulative_pnl_usd = 0.0
        actual_flags: list[dict] = []
        risk_exits: list[dict] = []
        order_diffs: list[str] = []
        last_candidate = ""
        seen_orders = 0

        now = start
        while now <= end:
            _set_quotes(svc, broker, now, tsla, tsll, tslz)
            result = run_once(broker=broker, market_data=svc, state=state, now=now)
            if state.provisional_flag:
                last_candidate = _flag_label(state.provisional_flag)
            new_orders = broker.orders[seen_orders:]
            seen_orders = len(broker.orders)
            if result.actions:
                for action in result.actions:
                    if action.startswith("ENTRY:") or action.startswith("OPPOSITE_SIGNAL:"):
                        direction = Direction(action.split(":", 1)[1])
                        actual_flags.append({
                            "order_time_et": _fmt(now),
                            "display_flag_et": _fmt(now - timedelta(minutes=3)),
                            "flag": _flag_label(direction),
                        })
                        sells = [o for o in new_orders if o.side == "SELL"]
                        buys = [o for o in new_orders if o.side == "BUY"]
                        if sells and open_trade is not None:
                            cumulative_pnl_usd = _close_trade(
                                open_trade, now=now, reason="반대플래그",
                                sell_price=sells[-1].executed_price, usdkrw=usdkrw,
                                budget_usd=budget_usd, cumulative_pnl_usd=cumulative_pnl_usd,
                            )
                            open_trade = None
                        if buys:
                            b = buys[-1]
                            open_trade = ReplayTrade(
                                trade_no=len(trades) + 1,
                                entry_reason="플래그",
                                entry_time_et=_fmt(now),
                                entry_time_kst=_fmt(now.astimezone(KST)),
                                candidate_flag=last_candidate or _flag_label(direction),
                                confirmed_flag=_flag_label(direction),
                                order_etf=b.symbol,
                                buy_price=round(float(b.executed_price), 4),
                                quantity=int(b.executed_qty),
                            )
                            trades.append(open_trade)
                    elif action.startswith("STOP_LOSS:") or action.startswith("PROFIT_LOCK:") or action.startswith("FORCED_LIQUIDATION:"):
                        reason = "손절" if action.startswith("STOP_LOSS:") else ("익절" if action.startswith("PROFIT_LOCK:") else "종료10분전 강제청산")
                        risk_exits.append({"time_et": _fmt(now), "reason": reason, "action": action})
                        sells = [o for o in new_orders if o.side == "SELL"]
                        if sells and open_trade is not None:
                            cumulative_pnl_usd = _close_trade(
                                open_trade, now=now, reason=reason,
                                sell_price=sells[-1].executed_price, usdkrw=usdkrw,
                                budget_usd=budget_usd, cumulative_pnl_usd=cumulative_pnl_usd,
                            )
                            open_trade = None
            now += timedelta(minutes=1)

        golden = _golden_trade_list()
        golden_regular = [g for g in golden if g["regular_session_order_allowed"]]
        signal_rows = ledger.load_signal_ledger(limit=10_000)
        execution_rows = ledger.load_execution_ledger(limit=10_000)
        actual_signal_flags = [
            {
                "display_flag_et": _fmt(pd.Timestamp(row["bar_start_at_et"]).to_pydatetime().astimezone(ET)),
                "order_due_et": _fmt(pd.Timestamp(row["bar_end_at_et"]).to_pydatetime().astimezone(ET)),
                "flag": _flag_label(Direction(row["direction"])),
                "order_result": row.get("order_result"),
                "block_reason": row.get("block_reason"),
                "order_etf": config.LONG_SYMBOL if Direction(row["direction"]) == Direction.UP_RED else config.INVERSE_SYMBOL,
            }
            for row in signal_rows
        ]
        actual_simple = [(row["display_flag_et"][11:16], row["flag"]) for row in actual_signal_flags]
        golden_simple = [(row["display_flag_et"][11:16], row["flag"]) for row in golden]
        if actual_simple != golden_simple:
            order_diffs.append(f"actual_flags={actual_simple} golden_flags={golden_simple}")
        after_close = [g for g in golden if not g["regular_session_order_allowed"]]
        if after_close:
            order_diffs.append("Golden contains after-regular-session flags blocked by TSLA_AUTO market session: " + str(after_close))
        blocked_flags = [row for row in actual_signal_flags if str(row.get("order_result")) == "BLOCKED"]
        if blocked_flags:
            order_diffs.append("Confirmed flags with no order: " + str(blocked_flags))
        root_causes = []
        if blocked_flags:
            root_causes.append("15:24 DOWN flag was confirmed, but BUY was blocked by the existing TSLA_AUTO stop-loss reentry cooldown.")
        if after_close:
            root_causes.append("16:09/16:27/16:57 ET Golden flags are after the 16:00 regular close; TSLA_AUTO blocks new entries after close.")

        report = {
            "budget_krw": budget_krw,
            "usdkrw": usdkrw,
            "budget_usd_used_by_tsla_auto": budget_usd,
            "data_rows": {
                "TSLA": int(len(tsla[tsla["datetime"].dt.date == TARGET_DAY])),
                config.LONG_SYMBOL: int(len(tsll)),
                config.INVERSE_SYMBOL: int(len(tslz)),
            },
            "1_Golden Trade List": golden,
            "2_actual_program_trade_list": [asdict(t) for t in trades],
            "3_flag_diff": order_diffs or [],
            "4_risk_exit_locations": risk_exits,
            "5_order_diff": order_diffs or [],
            "6_final_return_pct": round((cumulative_pnl_usd / budget_usd) * 100.0, 4) if budget_usd else 0.0,
            "7_root_cause": " ".join(root_causes) if root_causes else "Golden and TSLA_AUTO replay matched within the regular-session order rules.",
            "actual_flags": actual_signal_flags,
            "positions_after": [(p.symbol, p.quantity) for p in broker.get_positions()],
            "signal_ledger": signal_rows,
            "execution_ledger": execution_rows,
        }
        out = CACHE_DIR / "replay_20260731_golden_report.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"report_path={out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
