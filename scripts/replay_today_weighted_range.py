"""replay_today_weighted_range.py — 오늘자 실제 1분봉으로 weighted RANGE 매매 시나리오 재현.

KIS 분봉 API(FID_INPUT_HOUR_1)를 30분 간격으로 호출해 당일 전체 1분봉을
모은 뒤, 5초 샘플(분봉 내 선형 보간)로 Fast Worker 경로를 근사 재현한다:
  evaluate_range_weighted_entry → 진입/스케일인
  evaluate_weighted_range_probe_exit + swing 구조 이탈 → 청산

보수적 체결: 다음 1분봉 open + 추가 슬리피지(0.05%)를 병행 출력한다.

사용법:
    python scripts/replay_today_weighted_range.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.services import hynix_switch_engine as engine  # noqa: E402
from app.trading import early_trend_detector as etd  # noqa: E402
from app.trading import early_trend_live_feed as feed  # noqa: E402
from app.trading.etf_entry_confirmation import (  # noqa: E402
    compute_etf_breakouts,
    compute_etf_vwap,
    is_swing_structure_broken_against,
    resolve_window_directions,
    trade_aligned_window_directions,
)
from app.trading.hynix_fast_trend import compute_fast_trend_signal  # noqa: E402
from app.trading.hynix_symbols import (  # noqa: E402
    LONG_SYMBOL,
    SHORT_SYMBOL as INVERSE_SYMBOL,
    SIGNAL_SYMBOL,
)
from app.trading.hynix_switch_risk_gate import is_new_entry_allowed  # noqa: E402
from app.trading.trading_cost_engine import TradeCostEngine  # noqa: E402
from app.trading.range_weighted_optimize import (  # noqa: E402
    classify_intraday_regime,
    daily_loss_limit_reached,
    get_range_weighted_config,
    load_optimized_config,
)

INITIAL_CASH = 10_000_000.0
CONSERVATIVE_SLIPPAGE_PCT = 0.05
TODAY = datetime.now().strftime("%Y-%m-%d")

# 수정 직전(probe 잠금만, trade-align 미적용) 기준선
BASELINE_BEFORE = {
    "entries": 5,
    "round_trips": 5,
    "sub20_round_trips": 0,
    "net_pnl_krw": 69_219,
    "net_pnl_conservative_krw": -10_443,
    "return_pct": 0.692,
    "profit_factor": 4.85,
}
HOUR_ANCHORS = [
    f"{h:02d}{m:02d}00"
    for h in range(9, 16)
    for m in (0, 30)
    if not (h == 9 and m == 0)
] + ["153000"]


def fetch_full_day_1min(symbol: str, mode: str = "mock") -> pd.DataFrame:
    from app.trading.kis_client import create_kis_client

    client = create_kis_client(mode)
    if client is None:
        raise RuntimeError("KIS client unavailable")
    rows: dict[str, dict] = {}
    tr_id = "FHKST03010200"
    url = f"{client.base_url}/uapi/domestic-stock/v1/quotations/inquire-time-itemchartprice"
    for hour in HOUR_ANCHORS:
        headers = client._auth_headers(tr_id)
        params = {
            "FID_ETC_CLS_CODE": "",
            "FID_COND_MRKT_DIV_CODE": "J",
            "FID_INPUT_ISCD": symbol,
            "FID_INPUT_HOUR_1": hour,
            "FID_PW_DATA_INCU_YN": "N",
        }
        try:
            resp = client._get(url, headers=headers, params=params, timeout=(3, 12))
            resp.raise_for_status()
            for row in resp.json().get("output2", []):
                t = str(row.get("stck_cntg_hour") or "").zfill(6)
                close = float(row.get("stck_prpr") or 0)
                if close <= 0:
                    continue
                rows[t] = {
                    "time": t,
                    "open": float(row.get("stck_oprc") or close),
                    "high": float(row.get("stck_hgpr") or close),
                    "low": float(row.get("stck_lwpr") or close),
                    "close": close,
                    "volume": int(row.get("cntg_vol") or 0),
                }
        except Exception:
            continue
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(list(rows.values()))
    df["datetime"] = pd.to_datetime(
        TODAY + " " + df["time"].str[:2] + ":" + df["time"].str[2:4] + ":" + df["time"].str[4:6],
        errors="coerce",
    )
    return df.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)


def _merge_fixture_hynix(df: pd.DataFrame) -> pd.DataFrame:
    fixture = ROOT / "tests" / "_fixtures" / "hynix_20260721_last30_minute.csv"
    if not fixture.exists():
        return df
    fix = pd.read_csv(fixture)
    fix["datetime"] = pd.to_datetime(fix["datetime"])
    if df.empty:
        return fix.sort_values("datetime").reset_index(drop=True)
    merged = pd.concat([df, fix], ignore_index=True)
    merged = merged.drop_duplicates(subset=["datetime"], keep="last").sort_values("datetime")
    return merged.reset_index(drop=True)


def _price_at(df: pd.DataFrame, ts: datetime) -> float | None:
    if df is None or df.empty:
        return None
    minute = ts.replace(second=0, microsecond=0)
    row = df[df["datetime"] == minute]
    if row.empty:
        return None
    r = row.iloc[0]
    sec = ts.second + ts.microsecond / 1_000_000.0
    frac = min(1.0, sec / 59.0) if sec > 0 else 0.0
    return float(r["open"] + (r["close"] - r["open"]) * frac)


def _slice_to(df: pd.DataFrame, ts: datetime) -> pd.DataFrame:
    return df[df["datetime"] <= ts.replace(second=0, microsecond=0)].copy()


def _next_minute_open(df: pd.DataFrame, ts: datetime) -> float | None:
    minute = ts.replace(second=0, microsecond=0) + timedelta(minutes=1)
    row = df[df["datetime"] == minute]
    if row.empty:
        return None
    return float(row.iloc[0]["open"])


def _conservative_fill_price(df: pd.DataFrame, ts: datetime, side: str) -> float | None:
    base = _next_minute_open(df, ts)
    if base is None:
        return None
    slip = CONSERVATIVE_SLIPPAGE_PCT / 100.0
    if side == "BUY":
        return base * (1.0 + slip)
    return base * (1.0 - slip)


def _enhanced_decision(hynix_df: pd.DataFrame, direction: str | None) -> dict:
    if hynix_df is None or len(hynix_df) < 5:
        return {"final_action": "HOLD", "enhanced_score": 50.0, "inverse_pressure_score": 50.0}
    fast = compute_fast_trend_signal(hynix_df, now=hynix_df["datetime"].iloc[-1].to_pydatetime())
    d = fast.get("direction")
    if d == "UP":
        return {"final_action": "HYNIX_BUY", "enhanced_score": 72.0, "inverse_pressure_score": 28.0}
    if d == "DOWN":
        return {"final_action": "INVERSE_BUY", "enhanced_score": 28.0, "inverse_pressure_score": 72.0}
    if direction == "UP":
        return {"final_action": "HYNIX_BUY", "enhanced_score": 65.0, "inverse_pressure_score": 35.0}
    if direction == "DOWN":
        return {"final_action": "INVERSE_BUY", "enhanced_score": 35.0, "inverse_pressure_score": 65.0}
    return {"final_action": "HOLD", "enhanced_score": 50.0, "inverse_pressure_score": 50.0}


def run_replay(
    hynix_1m: pd.DataFrame,
    long_1m: pd.DataFrame,
    inverse_1m: pd.DataFrame,
) -> dict:
    hynix_3m = (
        hynix_1m.set_index("datetime")
        .resample("3min")
        .agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"})
        .dropna(subset=["close"])
        .reset_index()
    )
    start = max(hynix_1m["datetime"].min(), long_1m["datetime"].min(), inverse_1m["datetime"].min())
    end = min(hynix_1m["datetime"].max(), long_1m["datetime"].max(), inverse_1m["datetime"].max())
    start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
    end = end.replace(second=0, microsecond=0)

    day_regime = classify_intraday_regime(hynix_1m)
    cfg = get_range_weighted_config()
    cost_engine = TradeCostEngine()
    cash = INITIAL_CASH
    cash_conservative = INITIAL_CASH
    peak_equity = INITIAL_CASH
    peak_equity_conservative = INITIAL_CASH
    realized_pnl = 0.0
    realized_pnl_conservative = 0.0
    daily_loss_breached = False
    position = None
    position_conservative = None
    history: dict = {}
    continuation: dict = {}
    episode_entries: set[str] = set()
    trades: list[dict] = []
    trades_conservative: list[dict] = []
    events: list[dict] = []
    duplicate_episode = 0
    blocked_probe = 0
    blocked_reversal_repeat = 0

    ts = start
    while ts <= end:
        if not is_new_entry_allowed(ts):
            ts += timedelta(seconds=5)
            continue

        sp = _price_at(hynix_1m, ts)
        lp = _price_at(long_1m, ts)
        ip = _price_at(inverse_1m, ts)
        if sp is None or lp is None or ip is None:
            ts += timedelta(seconds=5)
            continue

        history = feed.record_price_sample(history, SIGNAL_SYMBOL, sp, ts)
        history = feed.record_price_sample(history, LONG_SYMBOL, lp, ts)
        history = feed.record_price_sample(history, INVERSE_SYMBOL, ip, ts)

        live_trade = feed.compute_live_trade_direction(
            history, ts, signal_symbol=SIGNAL_SYMBOL, long_symbol=LONG_SYMBOL, inverse_symbol=INVERSE_SYMBOL,
        )
        live_dir = live_trade.get("direction")
        if live_dir not in ("UP", "DOWN"):
            ts += timedelta(seconds=5)
            continue

        desired_symbol = LONG_SYMBOL if live_dir == "UP" else INVERSE_SYMBOL
        current_etf_price = lp if desired_symbol == LONG_SYMBOL else ip
        h_slice = _slice_to(hynix_1m, ts)
        etf_slice = _slice_to(long_1m if desired_symbol == LONG_SYMBOL else inverse_1m, ts)

        # Match production: episode/opposite helpers use trade-aligned dirs;
        # evaluate_range_weighted_entry expects price-space (raw) dirs.
        confirm_dirs_raw = resolve_window_directions(
            feed.compute_live_direction(history, desired_symbol, ts)
        )
        confirm_dirs = trade_aligned_window_directions(confirm_dirs_raw, symbol=desired_symbol)
        oppose_symbol = INVERSE_SYMBOL if desired_symbol == LONG_SYMBOL else LONG_SYMBOL
        oppose_dirs_raw = resolve_window_directions(
            feed.compute_live_direction(history, oppose_symbol, ts)
        )
        oppose_dirs = trade_aligned_window_directions(oppose_dirs_raw, symbol=oppose_symbol)
        signal_dirs = resolve_window_directions(feed.compute_live_direction(history, SIGNAL_SYMBOL, ts))

        vwap = compute_etf_vwap(etf_slice) if len(etf_slice) >= 3 else None
        confirm_above_vwap = bool(vwap is not None and current_etf_price >= float(vwap))
        breakouts = compute_etf_breakouts(etf_slice, current_etf_price, live_dir) if len(etf_slice) >= 3 else {}
        swing_breakout = bool(
            breakouts.get("recent_high") and current_etf_price > float(breakouts["recent_high"])
            if live_dir == "UP"
            else breakouts.get("recent_low") and current_etf_price < float(breakouts["recent_low"])
        )

        fast = compute_fast_trend_signal(h_slice, now=ts)
        returns = fast.get("returns") or {}
        expected_move = max(abs(float(returns.get(k) or 0.0)) for k in ("1m", "3m", "5m")) or 0.45
        cost_gate = etd.evaluate_cost_gate(desired_symbol, expected_move)
        decision = _enhanced_decision(h_slice, live_dir)

        macd_conf = engine._macd_williams_confirmation(etf_slice, live_dir)

        _existing_episode_direction = continuation.get("direction")
        _vwap_by_symbol = dict(continuation.get("prev_above_vwap_by_symbol") or {})
        _prev_above_vwap = _vwap_by_symbol.get(desired_symbol)
        vwap_reclaim = bool(
            confirm_above_vwap
            and _prev_above_vwap is False
            and confirm_dirs.get(5) == live_dir
            and confirm_dirs.get(10) == live_dir
        )
        _existing_structure_broken = False
        if _existing_episode_direction and _existing_episode_direction != live_dir:
            existing_symbol = LONG_SYMBOL if _existing_episode_direction == "UP" else INVERSE_SYMBOL
            existing_df = long_1m if existing_symbol == LONG_SYMBOL else inverse_1m
            existing_price = lp if existing_symbol == LONG_SYMBOL else ip
            existing_slice = _slice_to(existing_df, ts)
            if existing_price and len(existing_slice) >= 3:
                # Inverse is held long for market DOWN — use trade-aligned UP.
                structure_dir = "UP" if existing_symbol == INVERSE_SYMBOL else _existing_episode_direction
                _existing_structure_broken = is_swing_structure_broken_against(
                    existing_slice, existing_price, structure_dir,
                )
        _opposite_episode_confirmed = engine.detect_opposite_episode_transition(
            existing_direction=_existing_episode_direction,
            new_direction=live_dir,
            live_direction_matches=True,
            confirm_dirs=confirm_dirs,
            existing_structure_broken=_existing_structure_broken,
            new_etf_vwap_reclaim=vwap_reclaim,
            new_swing_breakout=swing_breakout,
        )
        direction_episode_changed = False
        if continuation.get("direction") != live_dir and (
            not _existing_episode_direction or _opposite_episode_confirmed
        ):
            direction_episode_changed = True
            engine.reset_range_episode_probe_state(
                continuation,
                now=ts,
                direction=live_dir,
                episode_id=f"{live_dir}:{ts.isoformat()}",
                reference_price=current_etf_price,
            )

        continuation["prev_above_vwap"] = confirm_above_vwap
        _vwap_by_symbol[desired_symbol] = confirm_above_vwap
        continuation["prev_above_vwap_by_symbol"] = _vwap_by_symbol
        engine.update_range_episode_structural_events(
            continuation,
            now=ts,
            swing_breakout=swing_breakout,
            vwap_reclaim=vwap_reclaim,
        )

        moved_pct = None
        if continuation.get("reference_price"):
            moved_pct = abs(current_etf_price / float(continuation["reference_price"]) - 1.0) * 100.0

        entry_eval = engine.evaluate_range_weighted_entry(
            decision=decision,
            direction=live_dir,
            live_direction=live_dir,
            signal_window_directions=signal_dirs,
            confirm_window_directions=confirm_dirs_raw,
            oppose_window_directions=oppose_dirs_raw,
            confirm_above_vwap=confirm_above_vwap,
            data_age_seconds=2.0,
            moved_pct_since_signal=moved_pct,
            expected_move_pct=expected_move,
            cost_pct=cost_gate.get("cost_pct"),
            expected_mfe_pct=expected_move,
            expected_mae_pct=abs(float(etd.FIXED_EARLY_STOP_PCT)),
            ema_slope_aligned=True,
            structure_confirmed=swing_breakout,
            structural_direction=compute_fast_trend_signal(h_slice, now=ts).get("direction"),
            entry_path_hint=None,
            day_regime=day_regime,
            range_config=cfg,
        )

        # ── 청산 ──
        if position is not None:
            held_price = lp if position["symbol"] == LONG_SYMBOL else ip
            held_df = long_1m if position["symbol"] == LONG_SYMBOL else inverse_1m
            held_slice = _slice_to(held_df, ts)
            net_ret = (held_price / position["entry_price"] - 1.0) * 100.0
            position["peak_net"] = max(position.get("peak_net", 0.0), net_ret)
            held_dirs = trade_aligned_window_directions(
                resolve_window_directions(
                    feed.compute_live_direction(history, position["symbol"], ts)
                ),
                symbol=position["symbol"],
            )
            structure_broken = is_swing_structure_broken_against(held_slice, held_price, position["direction"])
            etf_aligned = held_dirs.get(5) == position["direction"] and held_dirs.get(10) == position["direction"]
            h_fast = compute_fast_trend_signal(h_slice, now=ts)
            regime_reversal = (
                h_fast.get("direction") in ("UP", "DOWN")
                and h_fast.get("direction") != position["direction"]
                and held_dirs.get(5) != position["direction"]
                and held_dirs.get(10) != position["direction"]
            )

            _is_reversal_probe = (
                continuation.get("entry_path") == "REVERSAL"
                and not continuation.get("scale_in_done")
                and not continuation.get("probe_promoted_at")
            )
            if _is_reversal_probe:
                exit_plan = engine.evaluate_weighted_range_probe_exit(
                    continuation=continuation,
                    probe_direction=position["direction"],
                    structure_reversal_confirmed=structure_broken,
                    held_window_dirs=held_dirs,
                    macd_confirmed=bool(macd_conf.get("confirmed")),
                    etf_direction_aligned=etf_aligned,
                    now=ts,
                    net_return_pct=net_ret,
                    hard_stop_pct=float(etd.FIXED_EARLY_STOP_PCT),
                )
                if exit_plan.get("action") == "PROMOTE_CONTINUATION":
                    engine.promote_reversal_probe_to_continuation(continuation, now=ts)
                    position["entry_path"] = "CONTINUATION"
                    continuation["entry_path"] = "CONTINUATION"
                    exit_plan = {"action": "HOLD", "ratio": 0.0, "reason": exit_plan.get("reason")}
            elif continuation.get("entry_path") == "CONTINUATION" or continuation.get("probe_promoted_at"):
                exit_plan = engine.evaluate_weighted_continuation_exit(
                    net_return_pct=net_ret,
                    hard_stop_pct=float(etd.FIXED_EARLY_STOP_PCT),
                    structure_reversal_confirmed=structure_broken,
                    regime_reversal_confirmed=regime_reversal,
                    held_window_dirs=held_dirs,
                    position_direction=position["direction"],
                    tp1_taken=bool(position.get("tp1_taken")),
                    tp2_taken=bool(position.get("tp2_taken")),
                    confirmed_regime=etd.REGIME_FAST_REVERSAL_RANGE,
                )
            else:
                held_rev = {w: held_dirs.get(w) == "DOWN" for w in (5, 10, 20, 30)}
                opposite_cp = live_dir != position["direction"]
                exit_plan = etd.should_exit_probe(
                    net_return_pct=net_ret,
                    seconds_since_last_reconfirmation=5.0,
                    signal_still_valid=live_dir == position["direction"],
                    opposite_change_point=opposite_cp,
                    confirmed_regime=etd.REGIME_FAST_REVERSAL_RANGE,
                    opposite_live_seconds=10.0 if opposite_cp else 0.0,
                    position_direction=position["direction"],
                    held_etf_reversal_windows=held_rev,
                    opposite_etf_5s10s_confirmed=oppose_dirs.get(5) == "UP" and oppose_dirs.get(10) == "UP",
                    structure_reversal_confirmed=structure_broken,
                    peak_net_return_pct=position.get("peak_net", 0.0),
                )

            if exit_plan["action"] in ("SELL_ALL", "SELL_PARTIAL"):
                if exit_plan["action"] == "SELL_PARTIAL":
                    if "TP2" in str(exit_plan.get("reason") or ""):
                        position["tp2_taken"] = True
                    else:
                        position["tp1_taken"] = True
                sell_qty = position["qty"] if exit_plan["action"] == "SELL_ALL" else max(1, int(position["qty"] * exit_plan["ratio"]))
                gross = (held_price - position["entry_price"]) * sell_qty
                cost = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position["entry_price"] * sell_qty
                net = gross - cost
                cash += sell_qty * held_price
                realized_pnl += net
                held_sec = (ts - position["entry_time"]).total_seconds()
                trades.append({
                    "side": "SELL",
                    "symbol": position["symbol"],
                    "time": ts,
                    "price": held_price,
                    "qty": sell_qty,
                    "net_pnl": net,
                    "gross_pnl": gross,
                    "cost": cost,
                    "held_seconds": held_sec,
                    "reason": exit_plan.get("reason"),
                })
                if position_conservative is not None:
                    cons_price = _conservative_fill_price(held_df, ts, "SELL") or held_price
                    gross_c = (cons_price - position_conservative["entry_price"]) * sell_qty
                    cost_c = cost_engine.compute_round_trip_cost_pct(position["symbol"]) / 100.0 * position_conservative["entry_price"] * sell_qty
                    net_c = gross_c - cost_c
                    cash_conservative += sell_qty * cons_price
                    realized_pnl_conservative += net_c
                    trades_conservative.append({
                        "side": "SELL", "symbol": position["symbol"], "time": ts,
                        "price": cons_price, "qty": sell_qty, "net_pnl": net_c,
                        "held_seconds": held_sec, "reason": exit_plan.get("reason"),
                    })
                    position_conservative["qty"] -= sell_qty
                    if position_conservative["qty"] <= 0:
                        position_conservative = None
                events.append({
                    "time": ts.strftime("%H:%M:%S"),
                    "action": "매도",
                    "symbol": "레버리지" if position["symbol"] == LONG_SYMBOL else "인버스",
                    "price": held_price,
                    "qty": sell_qty,
                    "reason": exit_plan.get("reason"),
                    "net_ret_pct": round(net_ret, 3),
                })
                position["qty"] -= sell_qty
                if position["qty"] <= 0:
                    position = None
                    exit_reason = str(exit_plan.get("reason") or "")
                    if continuation.get("entry_path") == "REVERSAL":
                        engine.mark_range_probe_exit(
                            continuation,
                            now=ts,
                            entry_path=continuation.get("entry_path"),
                            reason=exit_reason,
                            probe_failed=bool(exit_plan.get("probe_failed")),
                        )
                    else:
                        engine.mark_range_episode_exit_awaiting_structure(
                            continuation,
                            now=ts,
                            reason=exit_reason,
                        )
            peak_equity = max(peak_equity, cash)
            peak_equity_conservative = max(peak_equity_conservative, cash_conservative)
            # Match live: daily loss uses actual realized PnL, not conservative-fill stress PnL.
            daily_loss_breached = daily_loss_limit_reached(
                realized_pnl, INITIAL_CASH, cfg
            )

        # ── 신규 진입 ──
        if position is None and entry_eval.get("action") == "ENTER":
            if daily_loss_limit_reached(realized_pnl, INITIAL_CASH, cfg):
                daily_loss_breached = True
                ts += timedelta(seconds=5)
                continue
            daily_loss_breached = False
            if (
                continuation.get("direction")
                and live_dir != continuation.get("direction")
                and not _opposite_episode_confirmed
            ):
                ts += timedelta(seconds=5)
                continue
            ep_id = continuation.get("direction_episode_id") or f"{live_dir}:{ts.isoformat()}"
            entry_path = entry_eval.get("entry_path")
            allows, block_reason = engine.range_episode_allows_entry(
                continuation,
                entry_path=entry_path,
                swing_breakout=swing_breakout,
                vwap_reclaim=vwap_reclaim,
                direction_changed=direction_episode_changed,
            )
            if ep_id in episode_entries and entry_path == "REVERSAL":
                duplicate_episode += 1
            elif not allows:
                blocked_probe += 1
                if block_reason == "REVERSAL_PROBE_ONCE_PER_EPISODE":
                    blocked_reversal_repeat += 1
            else:
                episode_entries.add(ep_id)
                target_pct = float(entry_eval.get("target_pct") or 0.25)
                qty = max(1, int(cash * target_pct / current_etf_price))
                if qty * current_etf_price <= cash:
                    cash -= qty * current_etf_price
                    position = {
                        "symbol": desired_symbol,
                        "direction": live_dir,
                        "qty": qty,
                        "entry_price": current_etf_price,
                        "entry_time": ts,
                        "peak_net": 0.0,
                        "entry_path": entry_path,
                    }
                    continuation["entry_done"] = True
                    continuation["entry_path"] = entry_path
                    continuation["macd_williams_confirmation"] = macd_conf
                    engine.mark_range_reversal_probe_entered(continuation, now=ts, entry_path=entry_path)
                    cons_entry = _conservative_fill_price(
                        long_1m if desired_symbol == LONG_SYMBOL else inverse_1m, ts, "BUY"
                    )
                    if cons_entry:
                        cons_qty = max(1, int(cash_conservative * target_pct / cons_entry))
                        if cons_qty * cons_entry <= cash_conservative:
                            cash_conservative -= cons_qty * cons_entry
                            position_conservative = {
                                "symbol": desired_symbol,
                                "direction": live_dir,
                                "qty": cons_qty,
                                "entry_price": cons_entry,
                                "entry_time": ts,
                                "entry_path": entry_path,
                            }
                    events.append({
                        "time": ts.strftime("%H:%M:%S"),
                        "action": "매수",
                        "symbol": "레버리지" if desired_symbol == LONG_SYMBOL else "인버스",
                        "price": current_etf_price,
                        "qty": qty,
                        "pct": round(target_pct * 100, 1),
                        "path": entry_path,
                        "label": entry_eval.get("structural_signal_label"),
                        "evidence": entry_eval.get("evidence_score"),
                    })

        # ── MACD 스케일인 ──
        elif (
            position is not None
            and position["symbol"] == desired_symbol
            and continuation.get("entry_done")
            and not continuation.get("scale_in_done")
            and macd_conf.get("confirmed")
        ):
            elapsed = (ts - datetime.fromisoformat(continuation["first_detected_at"])).total_seconds()
            if 10.0 <= elapsed <= 20.0:
                target_pct = 0.50
                add_qty = max(0, int(cash * target_pct / current_etf_price))
                if add_qty >= 1 and add_qty * current_etf_price <= cash:
                    cash -= add_qty * current_etf_price
                    position["qty"] += add_qty
                    continuation["scale_in_done"] = True
                    events.append({
                        "time": ts.strftime("%H:%M:%S"),
                        "action": "추가매수(MACD확인)",
                        "symbol": "레버리지" if desired_symbol == LONG_SYMBOL else "인버스",
                        "price": current_etf_price,
                        "qty": add_qty,
                        "pct": 50.0,
                    })

        ts += timedelta(seconds=5)

    # EOD 청산
    for label, pos, c_var, t_var, df_map in (
        ("optimistic", position, cash, trades, {LONG_SYMBOL: long_1m, INVERSE_SYMBOL: inverse_1m}),
        ("conservative", position_conservative, cash_conservative, trades_conservative, {LONG_SYMBOL: long_1m, INVERSE_SYMBOL: inverse_1m}),
    ):
        if pos is None:
            continue
        held_df = df_map[pos["symbol"]]
        held_price = _price_at(held_df, end) or float(held_df.iloc[-1]["close"])
        if label == "conservative":
            held_price = _conservative_fill_price(held_df, end, "SELL") or held_price
        gross = (held_price - pos["entry_price"]) * pos["qty"]
        cost = cost_engine.compute_round_trip_cost_pct(pos["symbol"]) / 100.0 * pos["entry_price"] * pos["qty"]
        net = gross - cost
        c_var += pos["qty"] * held_price
        t_var.append({
            "side": "SELL", "symbol": pos["symbol"], "time": end, "price": held_price,
            "qty": pos["qty"], "net_pnl": net, "gross_pnl": gross, "cost": cost,
            "held_seconds": (end - pos["entry_time"]).total_seconds(), "reason": "EOD",
        })
        if label == "optimistic":
            cash = c_var
            trades = t_var
        else:
            cash_conservative = c_var
            trades_conservative = t_var

    net_pnls = [t["net_pnl"] for t in trades if t["side"] == "SELL"]
    gross_profit = sum(v for v in net_pnls if v > 0)
    gross_loss = -sum(v for v in net_pnls if v < 0)
    pf = (gross_profit / gross_loss) if gross_loss > 0 else None
    round_trips = len([e for e in events if e["action"] == "매수"])
    sub20 = len([
        t for t in trades
        if t["side"] == "SELL" and t.get("held_seconds", 999) < 20 and t.get("reason") != "EOD"
    ])
    final_equity = cash

    net_pnls_c = [t["net_pnl"] for t in trades_conservative if t["side"] == "SELL"]
    gp_c = sum(v for v in net_pnls_c if v > 0)
    gl_c = -sum(v for v in net_pnls_c if v < 0)
    pf_c = (gp_c / gl_c) if gl_c > 0 else None
    final_equity_c = cash_conservative

    return {
        "events": events,
        "trades": trades,
        "trades_conservative": trades_conservative,
        "round_trips": round_trips,
        "entries": len([e for e in events if e["action"] == "매수"]),
        "net_pnl_krw": final_equity - INITIAL_CASH,
        "net_pnl_conservative_krw": final_equity_c - INITIAL_CASH,
        "return_pct": (final_equity / INITIAL_CASH - 1.0) * 100.0,
        "return_pct_conservative": (final_equity_c / INITIAL_CASH - 1.0) * 100.0,
        "profit_factor": pf,
        "profit_factor_conservative": pf_c,
        "duplicate_episode": duplicate_episode,
        "blocked_probe": blocked_probe,
        "blocked_reversal_repeat": blocked_reversal_repeat,
        "sub20_round_trips": sub20,
        "sub20_pct": (sub20 / round_trips * 100.0) if round_trips else 0.0,
        "hynix_1m_rows": len(hynix_1m),
        "hynix_3m_rows": len(hynix_3m),
        "period": (start, end),
        "day_regime": day_regime,
        "daily_loss_breached": daily_loss_breached,
        "max_intraday_dd_pct": ((min(cash, peak_equity) / peak_equity - 1.0) * 100.0) if peak_equity else 0.0,
        "max_intraday_dd_pct_conservative": (
            ((cash_conservative / peak_equity_conservative - 1.0) * 100.0) if peak_equity_conservative else 0.0
        ),
    }


def main() -> int:
    load_optimized_config()
    print("=" * 72)
    print(f"오늘({TODAY}) weighted RANGE 시나리오 재현")
    print("=" * 72)
    print("KIS에서 1분봉 수집 중...")
    hynix = fetch_full_day_1min(SIGNAL_SYMBOL)
    long_df = fetch_full_day_1min(LONG_SYMBOL)
    inv_df = fetch_full_day_1min(INVERSE_SYMBOL)
    hynix = _merge_fixture_hynix(hynix)
    print(f"  하이닉스 1분봉: {len(hynix)}봉  {hynix['datetime'].min()} ~ {hynix['datetime'].max()}")
    print(f"  레버리지 1분봉: {len(long_df)}봉  {long_df['datetime'].min()} ~ {long_df['datetime'].max()}")
    print(f"  인버스 1분봉: {len(inv_df)}봉  {inv_df['datetime'].min()} ~ {inv_df['datetime'].max()}")

    if len(hynix) < 30 or len(long_df) < 30 or len(inv_df) < 30:
        print("ERROR: 분봉 데이터 부족 — KIS 수집 실패")
        return 1

    result = run_replay(hynix, long_df, inv_df)
    start, end = result["period"]
    print(f"\n재현 구간: {start.strftime('%H:%M')} ~ {end.strftime('%H:%M')} (5초 틱 근사)")
    print(f"하이닉스 3분봉: {result['hynix_3m_rows']}봉 (1분봉 resample)")
    print("-" * 72)
    print("거래 이벤트 (시간순)")
    print("-" * 72)
    for ev in result["events"]:
        extra = ""
        if ev["action"] == "매수":
            extra = f" | {ev.get('pct')}% | {ev.get('path')} | evidence={ev.get('evidence')} | {ev.get('label')}"
        elif "net_ret" in ev:
            extra = f" | 수익률 {ev.get('net_ret_pct'):+.2f}%"
        print(f"  {ev['time']}  {ev['action']:16s}  {ev['symbol']:6s}  "
              f"{ev['price']:,.0f}원 x{ev['qty']}  — {ev.get('reason', extra)}{extra if ev['action']!='매수' else extra}")

    print("-" * 72)
    print("요약 (선형보간 체결)")
    print("-" * 72)
    pf = result["profit_factor"]
    pf_txt = f"{pf:.2f}" if pf is not None else "∞"
    print(f"  신규 매수 횟수: {result['entries']}")
    print(f"  완결 라운드트립(매도): {result['round_trips']}")
    print(f"  동일 episode REVERSAL 반복 차단: {result['blocked_reversal_repeat']}")
    print(f"  probe/episode 진입 차단: {result['blocked_probe']}")
    print(f"  20초 미만 왕복: {result['sub20_round_trips']} ({result['sub20_pct']:.1f}%)")
    print(f"  순손익: {result['net_pnl_krw']:+,.0f} KRW")
    print(f"  수익률: {result['return_pct']:+.3f}%")
    print(f"  Profit Factor: {pf_txt}")
    print("-" * 72)
    print("보수적 체결 (다음 1분봉 open + 0.05% 슬리피지)")
    print("-" * 72)
    pf_c = result["profit_factor_conservative"]
    pf_c_txt = f"{pf_c:.2f}" if pf_c is not None else "∞"
    print(f"  순손익: {result['net_pnl_conservative_krw']:+,.0f} KRW")
    print(f"  수익률: {result['return_pct_conservative']:+.3f}%")
    print(f"  Profit Factor: {pf_c_txt}")
    print(f"  당일 regime: {result.get('day_regime')}  일손실한도(-0.8%) 도달: {result.get('daily_loss_breached')}")
    print("-" * 72)
    print("수정 전 vs 수정 후 비교")
    print("-" * 72)
    b = BASELINE_BEFORE
    print(f"  {'지표':<28} {'수정 전':>12} {'수정 후':>12}")
    print(f"  {'신규진입':<28} {b['entries']:>12} {result['entries']:>12}")
    print(f"  {'라운드트립':<28} {b['round_trips']:>12} {result['round_trips']:>12}")
    print(f"  {'20초 미만 왕복':<28} {b['sub20_round_trips']:>12} {result['sub20_round_trips']:>12}")
    sub20_b = b['sub20_round_trips'] / b['round_trips'] * 100 if b['round_trips'] else 0
    print(f"  {'20초 미만 비율(%)':<28} {sub20_b:>11.1f}% {result['sub20_pct']:>11.1f}%")
    print(f"  {'순손익(KRW)':<28} {b['net_pnl_krw']:>+12,.0f} {result['net_pnl_krw']:>+12,.0f}")
    print(f"  {'보수적 순손익(KRW)':<28} {'—':>12} {result['net_pnl_conservative_krw']:>+12,.0f}")
    pf_b = f"{b['profit_factor']:.2f}"
    print(f"  {'Profit Factor':<28} {pf_b:>12} {pf_txt:>12}")
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
