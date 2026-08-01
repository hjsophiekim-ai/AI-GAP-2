from __future__ import annotations

import json
import sys
import argparse
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import config, ledger, market_data as market_data_module, market_session, order_executor, state_store
from app.trading.tsla_auto.cost_engine import OverseasTradeCostEngine
from app.trading.tsla_auto.market_data import MarketDataService
from app.trading.tsla_auto.models import Direction, QuoteSnapshot
from app.trading.tsla_auto.signal_engine import calculate_macd, raw_crossover_direction, resample_completed_3m
from app.trading.tsla_auto import worker as worker_module
from app.trading.tsla_auto.worker import initialize_strategy_session, run_once
from scripts.tsla_auto_replay_historical import CACHE_DIR, _isolated_paths, _load_history, _price_at
from tests.tsla_auto.fake_broker import FakeBroker

ET = config.ET
KST = config.KST
TARGET_DAY = date(2026, 7, 31)
REPLAY_STEP_SEC = 60

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
    gross_pnl_usd: float = 0.0
    buy_fee_usd: float = 0.0
    sell_fee_usd: float = 0.0
    slippage_usd: float = 0.0
    fx_cost_usd: float = 0.0
    total_cost_usd: float = 0.0
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


def _regular_minutes(trading_day: date) -> list[datetime]:
    start = datetime.combine(trading_day, config.SESSION_OPEN, tzinfo=ET)
    end = datetime.combine(trading_day, config.REGULAR_CLOSE, tzinfo=ET)
    out: list[datetime] = []
    cur = start
    while cur < end:
        out.append(cur)
        cur += timedelta(minutes=1)
    return out


def _missing_regular_minutes(df: pd.DataFrame, trading_day: date) -> list[str]:
    present = {
        pd.Timestamp(x).to_pydatetime().astimezone(ET).replace(second=0, microsecond=0)
        for x in df["datetime"]
        if pd.Timestamp(x).to_pydatetime().astimezone(ET).date() == trading_day
    }
    return [_fmt(dt) for dt in _regular_minutes(trading_day) if dt not in present]


def _session_only(df: pd.DataFrame, trading_day: date) -> pd.DataFrame:
    bounds = market_session.session_boundaries(trading_day)
    work = df.copy()
    work["datetime"] = pd.to_datetime(work["datetime"], errors="coerce")
    dt = work["datetime"].dt.tz_convert(ET)
    return work.loc[(dt >= bounds.market_open_et) & (dt < bounds.market_close_et)].reset_index(drop=True)


def _raw_confirmed_flags(tsla_history: pd.DataFrame, trading_day: date) -> list[dict[str, Any]]:
    bounds = market_session.session_boundaries(trading_day)
    rows: list[dict[str, Any]] = []
    current_session = _session_only(tsla_history, trading_day)
    for bar_start in list(resample_completed_3m(current_session, now=bounds.market_close_et)["datetime"]):
        bar_start_dt = pd.Timestamp(bar_start).to_pydatetime().astimezone(ET)
        order_at = bar_start_dt + timedelta(minutes=3)
        window_1m = tsla_history.loc[pd.to_datetime(tsla_history["datetime"]).dt.tz_convert(ET) < order_at].copy()
        bars_3m = resample_completed_3m(window_1m, now=order_at)
        snap = calculate_macd(bars_3m)
        if snap is None or snap.bar_dt.astimezone(ET) != bar_start_dt:
            continue
        direction = raw_crossover_direction(snap.previous_diff, snap.current_diff)
        if direction is None:
            continue
        rows.append({
            "bar_start_et": _fmt(bar_start_dt),
            "bar_start_kst": _fmt(bar_start_dt.astimezone(KST)),
            "bar_end_et": _fmt(order_at),
            "bar_end_kst": _fmt(order_at.astimezone(KST)),
            "previous_macd": snap.previous_macd,
            "previous_signal": snap.previous_signal,
            "current_macd": snap.macd,
            "current_signal": snap.signal,
            "previous_diff": snap.previous_diff,
            "current_diff": snap.current_diff,
            "raw_crossover_direction": _flag_label(direction),
            "direction": direction.value,
            "order_etf": config.LONG_SYMBOL if direction == Direction.UP_RED else config.INVERSE_SYMBOL,
        })
    return rows


def _macd_detector_rows(tsla_history: pd.DataFrame, trading_day: date) -> list[dict[str, Any]]:
    bounds = market_session.session_boundaries(trading_day)
    rows: list[dict[str, Any]] = []
    current_session = _session_only(tsla_history, trading_day)
    session_bars = resample_completed_3m(current_session, now=bounds.market_close_et)
    for bar_start in list(session_bars["datetime"]):
        bar_start_dt = pd.Timestamp(bar_start).to_pydatetime().astimezone(ET)
        bar_end_dt = bar_start_dt + timedelta(minutes=3)
        window_1m = tsla_history.loc[pd.to_datetime(tsla_history["datetime"]).dt.tz_convert(ET) < bar_end_dt].copy()
        bars_3m = resample_completed_3m(window_1m, now=bar_end_dt)
        snap = calculate_macd(bars_3m)
        if snap is None or snap.bar_dt.astimezone(ET) != bar_start_dt:
            rows.append({
                "bar_start": bar_start_dt.isoformat(),
                "bar_end": bar_end_dt.isoformat(),
                "MACD": "",
                "Signal": "",
                "Histogram": "",
                "Previous Histogram": "",
                "Previous MACD": "",
                "Previous Signal": "",
                "UP crossover 여부": False,
                "DOWN crossover 여부": False,
                "candidate 생성 여부": False,
                "confirmed 생성 여부": False,
                "emit 여부": False,
                "emit이 안된 이유": "MACD_WARMUP_NOT_READY",
            })
            continue
        raw = raw_crossover_direction(snap.previous_diff, snap.current_diff)
        up = raw == Direction.UP_RED
        down = raw == Direction.DOWN_BLUE
        rows.append({
            "bar_start": bar_start_dt.isoformat(),
            "bar_end": bar_end_dt.isoformat(),
            "MACD": snap.macd,
            "Signal": snap.signal,
            "Histogram": snap.current_diff,
            "Previous Histogram": snap.previous_diff,
            "Previous MACD": snap.previous_macd,
            "Previous Signal": snap.previous_signal,
            "UP crossover 여부": up,
            "DOWN crossover 여부": down,
            "candidate 생성 여부": bool(raw),
            "confirmed 생성 여부": bool(raw),
            "emit 여부": bool(raw),
            "emit이 안된 이유": "" if raw else "NO_CROSSOVER",
        })
    return rows


def _write_macd_detector_csv(tsla_history: pd.DataFrame, trading_day: date) -> Path:
    rows = _macd_detector_rows(tsla_history, trading_day)
    out = CACHE_DIR / f"macd_detector_{trading_day:%Y%m%d}.csv"
    out.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows).to_csv(out, index=False, encoding="utf-8-sig")
    return out


def _parse_dt(raw: Any) -> datetime | None:
    if raw is None or str(raw).strip() == "":
        return None
    text = str(raw).strip()
    for suffix in (" EDT", " EST"):
        if text.endswith(suffix):
            try:
                return datetime.strptime(text[:-4], "%Y-%m-%d %H:%M:%S").replace(tzinfo=ET)
            except ValueError:
                return None
    try:
        return datetime.fromisoformat(text).astimezone(ET)
    except ValueError:
        try:
            return pd.Timestamp(raw).to_pydatetime().astimezone(ET)
        except Exception:
            return None


def _row_for_1524_down(signal_rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    for row in signal_rows:
        bar_start = _parse_dt(row.get("bar_start_at_et"))
        if bar_start and bar_start.strftime("%H:%M") == "15:24" and row.get("direction") == Direction.DOWN_BLUE.value:
            return row
    return None


def _cooldown_diag(row: dict[str, Any] | None, *, last_stop_loss_at: datetime | None = None) -> dict[str, Any]:
    if not row:
        return {
            "bar_start_at_et": "",
            "bar_end_at_et": "",
            "confirmed_at_et": "",
            "order_attempt_at_et": "",
            "order_result": "",
            "block_reason": "",
            "last_stop_loss_at": "",
            "cooldown_end_at": "",
            "elapsed_minutes_after_stop_loss": "",
            "strong_filter": {"enabled": "", "approved": "", "score": "", "required_score": ""},
            "daily_entry_count": "",
            "market_phase": "",
        }
    bar_start = _parse_dt(row.get("bar_start_at_et"))
    bar_end = _parse_dt(row.get("bar_end_at_et"))
    order_attempt = _parse_dt(row.get("detected_at_et")) or bar_end or _parse_dt(row.get("order_requested_at_et"))
    last_stop = _parse_dt(row.get("last_stop_loss_at")) or last_stop_loss_at
    cooldown_end = _parse_dt(row.get("cooldown_end_at"))
    if cooldown_end is None and last_stop is not None:
        cooldown_end = last_stop + timedelta(minutes=config.STOP_LOSS_REENTRY_COOLDOWN_MIN)
    elapsed = row.get("elapsed_minutes_after_stop_loss")
    if (elapsed is None or str(elapsed) == "") and last_stop is not None and order_attempt is not None:
        elapsed = round((order_attempt - last_stop).total_seconds() / 60.0, 6)
    return {
        "bar_start_at_et": _fmt(bar_start) if bar_start else "",
        "bar_end_at_et": _fmt(bar_end) if bar_end else "",
        "confirmed_at_et": _fmt(bar_end) if bar_end else "",
        "order_attempt_at_et": _fmt(order_attempt) if order_attempt else "",
        "order_result": row.get("order_result", ""),
        "block_reason": row.get("block_reason", ""),
        "last_stop_loss_at": _fmt(last_stop) if last_stop else "",
        "cooldown_end_at": _fmt(cooldown_end) if cooldown_end else "",
        "elapsed_minutes_after_stop_loss": elapsed,
        "strong_filter": {
            "enabled": row.get("strong_filter_enabled", ""),
            "approved": row.get("strong_approved", ""),
            "score": row.get("strong_score", ""),
            "required_score": row.get("strong_required_score", ""),
        },
        "daily_entry_count": row.get("daily_entry_count", ""),
        "market_phase": (market_session.get_us_market_state(order_attempt).phase.value if order_attempt else ""),
    }


def _signal_table(signal_rows: list[dict[str, Any]], execution_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    executions_by_signal: dict[str, list[dict[str, Any]]] = {}
    for row in execution_rows:
        executions_by_signal.setdefault(str(row.get("signal_id") or ""), []).append(row)
    out: list[dict[str, Any]] = []
    for row in signal_rows:
        direction = Direction(row["direction"])
        bar_start = _parse_dt(row.get("bar_start_at_et"))
        bar_end = _parse_dt(row.get("bar_end_at_et"))
        signal_id = str(row.get("signal_id") or "")
        out.append({
            "bar_start_et": _fmt(bar_start) if bar_start else "",
            "bar_start_kst": _fmt(bar_start.astimezone(KST)) if bar_start else "",
            "bar_end_et": _fmt(bar_end) if bar_end else "",
            "bar_end_kst": _fmt(bar_end.astimezone(KST)) if bar_end else "",
            "direction": _flag_label(direction),
            "previous_macd": row.get("previous_macd", ""),
            "previous_signal": row.get("previous_signal", ""),
            "current_macd": row.get("confirmed_macd", ""),
            "current_signal": row.get("confirmed_signal", ""),
            "previous_diff": row.get("previous_diff", ""),
            "current_diff": row.get("confirmed_diff", ""),
            "filter_score": row.get("strong_score", ""),
            "filter_required_score": row.get("strong_required_score", ""),
            "filter_result": row.get("strong_decision", ""),
            "filter_approved": row.get("strong_approved", ""),
            "order_result": row.get("order_result", ""),
            "block_reason": row.get("block_reason", ""),
            "order_etf": config.LONG_SYMBOL if direction == Direction.UP_RED else config.INVERSE_SYMBOL,
            "executions": executions_by_signal.get(signal_id, []),
        })
    return out


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
    cost_engine: OverseasTradeCostEngine,
) -> float:
    pnl = cost_engine.compute_net_pnl_usd(trade.buy_price, sell_price, trade.quantity)
    net = float(pnl["net_pnl_usd"])
    cumulative = cumulative_pnl_usd + net
    trade.exit_reason = reason
    trade.exit_time_et = _fmt(now.astimezone(ET))
    trade.exit_time_kst = _fmt(now.astimezone(KST))
    trade.sell_price = round(float(sell_price), 4)
    trade.gross_pnl_usd = round(float(pnl["gross_pnl_usd"]), 4)
    trade.buy_fee_usd = round(float(pnl["buy_cost_usd"]["fee_usd"]), 4)
    trade.sell_fee_usd = round(float(pnl["sell_cost_usd"]["fee_usd"]), 4)
    trade.slippage_usd = round(float(pnl["slippage_usd"]), 4)
    trade.fx_cost_usd = round(float(pnl["buy_cost_usd"]["fx_cost_usd"]) + float(pnl["sell_cost_usd"]["fx_cost_usd"]), 4)
    trade.total_cost_usd = round(float(pnl["total_cost_usd"]), 4)
    trade.pnl_usd = round(net, 4)
    trade.pnl_krw = round(net * usdkrw, 2)
    trade.cumulative_pnl_usd = round(cumulative, 4)
    trade.cumulative_pnl_krw = round(cumulative * usdkrw, 2)
    trade.cumulative_return_pct = round((cumulative / budget_usd) * 100.0, 4) if budget_usd else 0.0
    return cumulative


def _cost_breakdown(trades: list[ReplayTrade], cost_engine: OverseasTradeCostEngine) -> dict[str, float]:
    gross = buy_fee = sell_fee = fx = sec = finra = slippage = total = net = 0.0
    for trade in trades:
        if not trade.exit_time_et:
            continue
        pnl = cost_engine.compute_net_pnl_usd(trade.buy_price, trade.sell_price, trade.quantity)
        gross += float(pnl["gross_pnl_usd"])
        buy_fee += float(pnl["buy_cost_usd"]["fee_usd"])
        sell_fee += float(pnl["sell_cost_usd"]["fee_usd"])
        fx += float(pnl["buy_cost_usd"]["fx_cost_usd"]) + float(pnl["sell_cost_usd"]["fx_cost_usd"])
        sec += float(pnl["sell_cost_usd"]["sec_fee_usd"])
        finra += float(pnl["sell_cost_usd"]["finra_taf_usd"])
        slippage += float(pnl["slippage_usd"])
        total += float(pnl["total_cost_usd"])
        net += float(pnl["net_pnl_usd"])
    return {
        "gross_pnl_usd": round(gross, 4),
        "buy_commission_usd": round(buy_fee, 4),
        "sell_commission_usd": round(sell_fee, 4),
        "fx_cost_usd": round(fx, 4),
        "sec_fee_usd": round(sec, 4),
        "finra_taf_usd": round(finra, 4),
        "slippage_spread_usd": round(slippage, 4),
        "total_cost_usd": round(total, 4),
        "net_pnl_usd": round(net, 4),
    }


def _max_drawdown(trades: list[ReplayTrade]) -> float:
    peak = 0.0
    max_dd = 0.0
    for trade in trades:
        cur = float(trade.cumulative_pnl_usd)
        peak = max(peak, cur)
        max_dd = min(max_dd, cur - peak)
    return round(max_dd, 4)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strong-filter", action="store_true", help="simulate the UI strong-flag-only toggle as ON")
    parser.add_argument("--macd-detector-only", action="store_true", help="write every regular-session 3m MACD detector row and skip orders")
    args = parser.parse_args()
    usdkrw = _load_usdkrw()
    budget_krw = 9_500_000.0
    budget_usd = round(budget_krw / usdkrw, 2)
    replay_cost_engine = OverseasTradeCostEngine()
    tsla, tsll, tslz = _load_history(TARGET_DAY)
    if args.macd_detector_only:
        out = _write_macd_detector_csv(tsla, TARGET_DAY)
        rows = _macd_detector_rows(tsla, TARGET_DAY)
        emitted = [r for r in rows if bool(r["emit 여부"])]
        report = {
            "mode": "MACD_DETECTOR_ONLY",
            "orders_disabled": True,
            "session_boundaries": {
                "session_open_et": market_session.session_boundaries(TARGET_DAY).market_open_et.isoformat(),
                "session_close_et": market_session.session_boundaries(TARGET_DAY).market_close_et.isoformat(),
                "session_open_kst": market_session.session_boundaries(TARGET_DAY).market_open_et.astimezone(KST).isoformat(),
                "session_close_kst": market_session.session_boundaries(TARGET_DAY).market_close_et.astimezone(KST).isoformat(),
            },
            "timestamp_normalization": {
                "kis_parser_rule": "xymd/xhms are parsed as America/New_York; kymd/khms fallback is parsed as Asia/Seoul then converted to America/New_York",
                "raw_response_cached": False,
                "cache_first_regular_et": _session_only(tsla, TARGET_DAY)["datetime"].iloc[0].isoformat(),
                "cache_last_regular_et": _session_only(tsla, TARGET_DAY)["datetime"].iloc[-1].isoformat(),
            },
            "three_minute_bars": len(rows),
            "emitted_crossovers": len(emitted),
            "emitted_list": [
                {
                    "bar_start": r["bar_start"],
                    "bar_end": r["bar_end"],
                    "direction": "UP" if r["UP crossover 여부"] else "DOWN",
                    "previous_diff": r["Previous Histogram"],
                    "current_diff": r["Histogram"],
                }
                for r in emitted
            ],
            "csv_path": str(out),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        print(f"csv_path={out}")
        return 0

    with _isolated_paths():
        original_now_iso = order_executor._now_iso
        original_reconcile_retries = worker_module.ORDER_FILL_RECONCILE_RETRIES
        original_reconcile_delay = worker_module.ORDER_FILL_RECONCILE_DELAY_SEC
        worker_module.ORDER_FILL_RECONCILE_RETRIES = 1
        worker_module.ORDER_FILL_RECONCILE_DELAY_SEC = 0.0
        svc = MarketDataService(mode="MOCK", fetch_minute_candles=lambda *a: (tsla, {}), fetch_quote=lambda mode, symbol: (None, None))
        start = datetime.combine(TARGET_DAY, config.SESSION_OPEN, tzinfo=ET)
        end = datetime.combine(TARGET_DAY, config.REGULAR_CLOSE, tzinfo=ET)
        svc.bootstrap(now=start)
        state = state_store.default_state()
        state.auto_trade_on = True
        state.strong_filter_enabled = bool(args.strong_filter)
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
        profit_lock_exits: list[dict] = []
        order_diffs: list[str] = []
        last_candidate = ""
        seen_orders = 0

        now = start
        while now <= end:
            _set_quotes(svc, broker, now, tsla, tsll, tslz)
            order_executor._now_iso = lambda current=now: current.isoformat()
            pre_position = state.position
            pre_peak_return = float(state.peak_net_return or 0.0)
            pre_current_return = 0.0
            if pre_position is not None and int(pre_position.quantity or 0) > 0:
                pre_df = tsll if pre_position.symbol == config.LONG_SYMBOL else tslz
                pre_price = _price_at(pre_df, now)
                if pre_price > 0:
                    pnl = replay_cost_engine.compute_net_pnl_usd(
                        float(pre_position.avg_price), float(pre_price), int(pre_position.quantity)
                    )
                    pre_current_return = (
                        float(pnl["net_pnl_usd"]) / (float(pre_position.avg_price) * int(pre_position.quantity)) * 100.0
                    )
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
                                open_trade, now=now, reason="OPPOSITE_SIGNAL",
                                sell_price=sells[-1].executed_price, usdkrw=usdkrw,
                                budget_usd=budget_usd, cumulative_pnl_usd=cumulative_pnl_usd,
                                cost_engine=replay_cost_engine,
                            )
                            open_trade = None
                        if buys:
                            b = buys[-1]
                            open_trade = ReplayTrade(
                                trade_no=len(trades) + 1,
                                entry_reason="CONFIRMED_FLAG",
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
                        reason = "STOP_LOSS" if action.startswith("STOP_LOSS:") else ("PROFIT_LOCK" if action.startswith("PROFIT_LOCK:") else "FORCED_LIQUIDATION")
                        risk_exits.append({"time_et": _fmt(now), "reason": reason, "action": action})
                        if action.startswith("PROFIT_LOCK:"):
                            profit_lock_exits.append({
                                "time_et": _fmt(now),
                                "peak_return_pct": round(pre_peak_return, 6),
                                "current_return_pct": round(pre_current_return, 6),
                                "giveback_pct": round(pre_peak_return - pre_current_return, 6),
                            })
                        sells = [o for o in new_orders if o.side == "SELL"]
                        if sells and open_trade is not None:
                            cumulative_pnl_usd = _close_trade(
                                open_trade, now=now, reason=reason,
                                sell_price=sells[-1].executed_price, usdkrw=usdkrw,
                                budget_usd=budget_usd, cumulative_pnl_usd=cumulative_pnl_usd,
                                cost_engine=replay_cost_engine,
                            )
                            open_trade = None
            now += timedelta(seconds=REPLAY_STEP_SEC)
        order_executor._now_iso = original_now_iso
        worker_module.ORDER_FILL_RECONCILE_RETRIES = original_reconcile_retries
        worker_module.ORDER_FILL_RECONCILE_DELAY_SEC = original_reconcile_delay

        signal_rows = ledger.load_signal_ledger(limit=10_000)
        execution_rows = ledger.load_execution_ledger(limit=10_000)
        raw_flags = _raw_confirmed_flags(tsla, TARGET_DAY)
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
        raw_simple = [(row["bar_start_et"][11:16], _flag_label(Direction(row["direction"]))) for row in raw_flags]
        if actual_simple != raw_simple:
            order_diffs.append(f"actual_flags={actual_simple} raw_flags={raw_simple}")
        validations = {
            "raw_flag_count_equals_signal_ledger_count": len(raw_simple) == len(actual_simple),
            "raw_flag_list_equals_signal_ledger_list": raw_simple == actual_simple,
            "confirmed_crossover_has_no_filter_input_not_crossover": not any(
                str(row.get("block_reason") or "") == config.FILTER_INPUT_NOT_CROSSOVER
                or str(row.get("strong_decision") or "") == config.FILTER_INPUT_NOT_CROSSOVER
                for row in signal_rows
            ),
        }
        if not all(validations.values()):
            order_diffs.append(f"validation_failures={validations}")
        blocked_flags = [row for row in actual_signal_flags if str(row.get("order_result")) == "BLOCKED"]
        if blocked_flags:
            order_diffs.append("Confirmed flags with no order: " + str(blocked_flags))
        actual_causes = []
        for row in blocked_flags:
            actual_causes.append({
                "display_flag_et": row.get("display_flag_et"),
                "flag": row.get("flag"),
                "block_reason": row.get("block_reason"),
            })

        target_signal_row = _row_for_1524_down(signal_rows)
        target_signal_id = str((target_signal_row or {}).get("signal_id") or "")
        target_execution_rows = [r for r in execution_rows if str(r.get("signal_id") or "") == target_signal_id]
        target_order_at = _parse_dt((target_signal_row or {}).get("detected_at_et")) or _parse_dt((target_signal_row or {}).get("bar_end_at_et"))
        prior_stop_loss_times = [
            _parse_dt(t.exit_time_et)
            for t in trades
            if t.exit_reason == "STOP_LOSS" and _parse_dt(t.exit_time_et) and target_order_at and _parse_dt(t.exit_time_et) <= target_order_at
        ]
        last_stop_loss_at = max(prior_stop_loss_times) if prior_stop_loss_times else None
        missing_tslz = _missing_regular_minutes(tslz, TARGET_DAY)
        trade_event_minutes = {
            _parse_dt(t.entry_time_et).replace(second=0, microsecond=0) for t in trades if _parse_dt(t.entry_time_et)
        } | {
            _parse_dt(t.exit_time_et).replace(second=0, microsecond=0) for t in trades if _parse_dt(t.exit_time_et)
        }
        missing_tslz_dt = {_parse_dt(x).replace(second=0, microsecond=0) for x in missing_tslz if _parse_dt(x)}
        missing_impacts = sorted(_fmt(x) for x in (trade_event_minutes & missing_tslz_dt))
        costs = _cost_breakdown(trades, replay_cost_engine)
        total_pnl_krw = round(cumulative_pnl_usd * usdkrw, 2)
        final_return_pct = round((cumulative_pnl_usd / budget_usd) * 100.0, 4) if budget_usd else 0.0

        report = {
            "budget_krw": budget_krw,
            "usdkrw": usdkrw,
            "budget_usd_used_by_tsla_auto": budget_usd,
            "cost_basis": {
                "source": "app.trading.tsla_auto.cost_engine.OverseasTradeCostEngine",
                "buy_commission_rate": replay_cost_engine.fee_rate("BUY"),
                "sell_commission_rate": replay_cost_engine.fee_rate("SELL"),
                "fx_preference_rate": replay_cost_engine._cfg.get("fx_preference_rate"),
                "fx_effective_rate": replay_cost_engine.fx_effective_rate(),
                "fallback_one_way_slippage_rate": replay_cost_engine._slippage_rate("limit"),
            },
            "strong_filter_enabled": bool(state.strong_filter_enabled),
            "session_boundaries": {
                "session_open_et": market_session.session_boundaries(TARGET_DAY).market_open_et.isoformat(),
                "session_close_et": market_session.session_boundaries(TARGET_DAY).market_close_et.isoformat(),
                "session_open_kst": market_session.session_boundaries(TARGET_DAY).market_open_et.astimezone(KST).isoformat(),
                "session_close_kst": market_session.session_boundaries(TARGET_DAY).market_close_et.astimezone(KST).isoformat(),
            },
            "kis_timestamp_normalization": {
                "cache_source": "data/cache/tsla_auto/*_20260731_1m.csv",
                "raw_response_cached": False,
                "normalized_timezone": "America/New_York",
                "tsla_first_regular_et": _session_only(tsla, TARGET_DAY)["datetime"].iloc[0].isoformat(),
                "tsla_last_regular_et": _session_only(tsla, TARGET_DAY)["datetime"].iloc[-1].isoformat(),
            },
            "data_rows": {
                "TSLA": int(len(tsla[tsla["datetime"].dt.date == TARGET_DAY])),
                config.LONG_SYMBOL: int(len(tsll)),
                config.INVERSE_SYMBOL: int(len(tslz)),
            },
            "replay_timing": {
                "worker_interval_sec": REPLAY_STEP_SEC,
                "price_assumption": "Historical replay runs on 1-minute candles; within each 1-minute candle the latest 1-minute close is held constant.",
            },
            "tslz_missing_regular_1m": {
                "expected_rows": 390,
                "actual_rows": int(len(tslz)),
                "missing_count": len(missing_tslz),
                "missing_times_et": missing_tslz,
                "impacts_order_stop_profit_forced_times": missing_impacts,
            },
            "A_raw_macd_confirmed_flags": raw_flags,
            "B_strong_filter_off_order_results" if not state.strong_filter_enabled else "B_strong_filter_off_order_results": (
                _signal_table(signal_rows, execution_rows) if not state.strong_filter_enabled else []
            ),
            "C_strong_filter_on_score_results" if state.strong_filter_enabled else "C_strong_filter_on_score_results": (
                _signal_table(signal_rows, execution_rows) if state.strong_filter_enabled else []
            ),
            "validations": validations,
            "2_actual_program_trade_list": [asdict(t) for t in trades],
            "3_flag_diff": order_diffs or [],
            "4_risk_exit_locations": risk_exits,
            "5_order_diff": order_diffs or [],
            "6_final_return_pct": final_return_pct,
            "7_root_cause": actual_causes,
            "final_trade_table": {
                "trades": [asdict(t) for t in trades],
                "cost_breakdown": {
                    **costs,
                    "net_pnl_krw": total_pnl_krw,
                    "net_return_pct": final_return_pct,
                    "trade_count": len([t for t in trades if t.exit_time_et]),
                    "win_count": sum(1 for t in trades if t.exit_time_et and t.pnl_usd > 0),
                    "loss_count": sum(1 for t in trades if t.exit_time_et and t.pnl_usd <= 0),
                },
                "total_pnl_usd": round(cumulative_pnl_usd, 4),
                "total_pnl_krw": total_pnl_krw,
                "final_return_pct": final_return_pct,
            },
            "target_1524_down_diagnostics": _cooldown_diag(target_signal_row, last_stop_loss_at=last_stop_loss_at),
            "target_1524_down_signal_ledger": target_signal_row or {},
            "target_1524_down_execution_ledger": target_execution_rows,
            "profit_lock_exits": profit_lock_exits,
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
