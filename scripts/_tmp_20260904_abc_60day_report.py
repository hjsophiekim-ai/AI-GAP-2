"""raw.json(_tmp_20260904_abc_30day_compare.py 산출물) 기반 리포트 집계.
READ-ONLY, production 무관."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RAW = PROJECT_ROOT / "data" / "validation" / "abc_60day_compare" / "raw.json"
OUT = PROJECT_ROOT / "data" / "validation" / "abc_60day_compare" / "report.json"


def metrics(trades: list[dict], n_days: int) -> dict:
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    df = df.sort_values("exit_time").reset_index(drop=True)
    n = len(df)
    wins = df[df["net_pct"] > 0]
    losses = df[df["net_pct"] < 0]
    breakeven = df[df["net_pct"] == 0]
    win_rate = len(wins) / n * 100 if n else 0.0
    simple_cum = df["net_pct"].sum()
    equity = (1 + df["net_pct"] / 100).cumprod()
    compound_cum = (equity.iloc[-1] - 1) * 100
    avg_ret = df["net_pct"].mean()
    gross_profit = wins["net_pct"].sum()
    gross_loss = -losses["net_pct"].sum()
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    running_peak = equity.cummax()
    drawdown = (equity / running_peak - 1) * 100
    mdd = drawdown.min()
    daily = df.groupby("date")["net_pct"].sum()
    profit_days = int((daily > 0).sum())
    loss_days = int((daily < 0).sum())
    flat_days = int((daily == 0).sum())
    # 최대 연속손실 (거래 단위)
    streak = 0
    max_streak = 0
    streak_pnl = 0.0
    max_streak_pnl = 0.0
    cur_pnl = 0.0
    for v in df["net_pct"]:
        if v < 0:
            streak += 1
            cur_pnl += v
        else:
            streak = 0
            cur_pnl = 0.0
        if streak > max_streak:
            max_streak = streak
            max_streak_pnl = cur_pnl
    top10 = df.nlargest(min(10, n), "net_pct")
    rest = df.drop(top10.index)
    top10_excl_simple = rest["net_pct"].sum()
    if len(rest):
        top10_excl_equity = (1 + rest.sort_values("exit_time")["net_pct"] / 100).cumprod().iloc[-1]
        top10_excl_compound = (top10_excl_equity - 1) * 100
    else:
        top10_excl_compound = 0.0
    return {
        "trades": n,
        "avg_trades_per_day": round(n / n_days, 3),
        "wins": int(len(wins)), "losses": int(len(losses)), "breakeven": int(len(breakeven)),
        "win_rate_pct": round(win_rate, 2),
        "simple_cum_return_pct": round(float(simple_cum), 4),
        "compound_cum_return_pct": round(float(compound_cum), 4),
        "avg_return_per_trade_pct": round(float(avg_ret), 4),
        "profit_factor": round(float(pf), 4) if pf != float("inf") else None,
        "mdd_pct": round(float(mdd), 4),
        "profit_days": profit_days, "loss_days": loss_days, "flat_days": flat_days,
        "max_consecutive_losses_trades": int(max_streak),
        "max_consecutive_losses_cum_pct": round(float(max_streak_pnl), 4),
        "top10_excluded_simple_cum_pct": round(float(top10_excl_simple), 4),
        "top10_excluded_compound_cum_pct": round(float(top10_excl_compound), 4),
    }


def find_trade(trades: list[dict], entry_bar_idx: int, direction: str):
    for t in trades:
        if t["entry_bar_idx"] == entry_bar_idx and t["direction"] == direction:
            return t
    return None


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    dates = raw["dates"]
    n_days = len(dates)

    report = {
        "period": {"start": dates[0], "end": dates[-1], "n_days": n_days},
        "metrics": {
            "A": metrics(raw["A"], n_days),
            "B": metrics(raw["B"], n_days),
            "C": metrics(raw["C"], n_days),
        },
    }

    # ── B 조기익절 발동 거래 전부 vs A ──
    fired = [t for t in raw["B_frozen"] if t["lock_fired"]]
    diffs = []
    for t in fired:
        a_t = find_trade(raw["A"], t["entry_bar_idx"], t["direction"])
        diffs.append({
            "date": t["date"], "entry_time": t["entry_time"], "direction": t["direction"],
            "slot_number": t["slot_number"],
            "B_exit_time": t["exit_time"], "B_exit_reason": t["exit_reason"], "B_net_pct": t["net_pct"],
            "A_exit_time": a_t["exit_time"] if a_t else None,
            "A_exit_reason": a_t["exit_reason"] if a_t else None,
            "A_net_pct": a_t["net_pct"] if a_t else None,
            "diff_net_pct": round(t["net_pct"] - a_t["net_pct"], 4) if a_t else None,
        })
    report["B_early_tp_fired_vs_A"] = diffs
    report["B_early_tp_fired_count"] = len(fired)
    report["B_early_tp_fired_diff_sum_pct"] = round(
        sum(d["diff_net_pct"] for d in diffs if d["diff_net_pct"] is not None), 4)

    # ── C Slot1 TQ<4 차단 후보 + A/B 사후손익 ──
    dec_a = raw["decisions_A"]
    dec_b = raw["decisions_B"]
    blocked = []
    for blk in raw["slot1_blocks_C"]:
        dec_key = f"{blk['decision_idx']}|{blk['direction']}"
        a_dec = dec_a.get(dec_key)
        b_dec = dec_b.get(dec_key)
        a_t = find_trade(raw["A"], blk["decision_idx"], blk["direction"]) if a_dec and a_dec["approved"] else None
        b_t = find_trade(raw["B"], blk["decision_idx"], blk["direction"]) if b_dec and b_dec["approved"] else None
        blocked.append({
            "date": blk["date"], "decision_at": blk["decision_at"], "direction": blk["direction"],
            "tq_passed": blk["tq_passed"],
            "A_approved": bool(a_dec["approved"]) if a_dec else None,
            "A_outcome_net_pct": a_t["net_pct"] if a_t else None,
            "A_outcome_exit_reason": a_t["exit_reason"] if a_t else None,
            "A_outcome_peak_pct": a_t["peak_net_pct"] if a_t else None,
            "B_approved": bool(b_dec["approved"]) if b_dec else None,
            "B_outcome_net_pct": b_t["net_pct"] if b_t else None,
            "B_outcome_exit_reason": b_t["exit_reason"] if b_t else None,
            "B_outcome_peak_pct": b_t["peak_net_pct"] if b_t else None,
        })
    report["C_slot1_tq_blocked"] = blocked
    good_runner_threshold = 2.0
    missed_runners = [
        b for b in blocked
        if (b["A_outcome_net_pct"] is not None and b["A_outcome_net_pct"] >= good_runner_threshold)
        or (b["B_outcome_net_pct"] is not None and b["B_outcome_net_pct"] >= good_runner_threshold)
    ]
    report["C_missed_good_runners"] = missed_runners
    report["C_missed_good_runners_threshold_pct"] = good_runner_threshold

    # ── 최근 5영업일 날짜별 A/B/C 거래 ──
    last5 = dates[-5:]
    per_day = {}
    for d in last5:
        per_day[d] = {}
        for tag in ("A", "B", "C"):
            day_trades = [t for t in raw[tag] if t["date"] == d]
            day_trades = sorted(day_trades, key=lambda t: t["entry_time"])
            per_day[d][tag] = [
                {
                    "slot": t["slot_number"], "direction": t["direction"],
                    "entry_time": t["entry_time"], "entry_price": t["entry_price"],
                    "exit_time": t["exit_time"], "exit_price": t["exit_price"],
                    "exit_reason": t["exit_reason"], "net_pct": t["net_pct"],
                    "entry_chop": t.get("entry_chop"), "lock_fired": t.get("lock_fired"),
                    "slot1_tq_passed": t.get("slot1_tq_passed"),
                }
                for t in day_trades
            ]
    report["last5_days"] = per_day

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUT}")
    print(json.dumps(report["metrics"], ensure_ascii=False, indent=2))
    print("B early-tp fired:", report["B_early_tp_fired_count"],
          "diff sum:", report["B_early_tp_fired_diff_sum_pct"])
    print("C slot1 blocked:", len(blocked), "missed good runners:", len(missed_runners))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
