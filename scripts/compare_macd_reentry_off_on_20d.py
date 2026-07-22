"""MACD B + TP/SL: CONTINUATION_REENTRY OFF vs ON over ≥20 sessions.

Read-only. Does NOT flip live CONTINUATION_REENTRY_ENABLED.

Date list / 1m sources:
  1) Prefer sibling artifact dates from
     data/state/macd_vs_williams_early_20d_compare.json (or partial)
  2) Else reuse scripts.compare_macd_vs_williams_early_20d.build_day_universe
     (KIS replay cache → Naver fchart → synthetic_daily_anchor)

Usage:
    python scripts/compare_macd_reentry_off_on_20d.py
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pandas as pd  # noqa: E402

from app.trading.macd_hynix_strategy import (  # noqa: E402
    CONTINUATION_REENTRY_ENABLED,
    ENTRY_CONTINUATION,
    ENTRY_INITIAL,
    INVERSE_SYMBOL,
    LONG_SYMBOL,
    SIGNAL_SYMBOL,
    SL_NET_PCT,
    TP_NET_PCT,
)
from scripts.compare_macd_vs_williams_early_20d import (  # noqa: E402
    SYM_TAG,
    build_day_universe,
)
from scripts.replay_macd_hynix_tpsl_reentry_compare import (  # noqa: E402
    ADVERSE_PCT,
    DELAY_MIN,
    INITIAL_CASH,
    Trade,
    VariantResult,
    _metrics,
    replay_variant,
)

CACHE = ROOT / "data" / "cache"
STATE = ROOT / "data" / "state"
SIBLING_JSON = STATE / "macd_vs_williams_early_20d_compare.json"
SIBLING_PARTIAL = STATE / "macd_vs_williams_early_20d_partial.json"
OUT_JSON = STATE / "macd_reentry_off_on_20d_compare.json"
OUT_MD = STATE / "macd_reentry_off_on_20d_compare.md"
MIN_DAYS = 20


def _iso(day: str) -> str:
    day = str(day)
    if "-" in day:
        return day
    return f"{day[:4]}-{day[4:6]}-{day[6:8]}"


def _write_replay_cache(day: str, frames: dict[str, pd.DataFrame]) -> None:
    tag = day.replace("-", "")
    CACHE.mkdir(parents=True, exist_ok=True)
    for sym, df in frames.items():
        out = df.copy()
        out["datetime"] = pd.to_datetime(out["datetime"], errors="coerce")
        out = out.dropna(subset=["datetime"]).sort_values("datetime").reset_index(drop=True)
        if "time" not in out.columns:
            out["time"] = out["datetime"].dt.strftime("%H%M%S")
        path = CACHE / f"replay_{tag}_{SYM_TAG[sym]}_1m.csv"
        out.to_csv(path, index=False)


def _load_sibling_meta() -> Optional[dict[str, Any]]:
    for path in (SIBLING_JSON, SIBLING_PARTIAL):
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        days = payload.get("dates") or payload.get("trading_days") or payload.get("days")
        sources = payload.get("date_sources") or payload.get("day_sources") or {}
        if days and len(days) >= MIN_DAYS:
            return {
                "days": [_iso(d) for d in days],
                "date_sources": {_iso(k): str(v) for k, v in sources.items()},
                "from": str(path),
            }
    return None


def ensure_dataset(n_days: int = MIN_DAYS) -> dict[str, Any]:
    notes: list[str] = []
    sibling = _load_sibling_meta()

    # Always build universe with same helper so caches/sources match sibling script.
    dates, date_sources, day_data = build_day_universe(n_days, refetch_naver=False)

    if sibling:
        # Prefer sibling's exact date list when available; rebuild missing frames via universe merge.
        preferred = sibling["days"]
        notes.append(f"Reused sibling date list from {sibling['from']}.")
        # If sibling sources present, prefer those labels
        for d, src in sibling.get("date_sources", {}).items():
            date_sources[d] = src
        # Ensure every preferred day exists in day_data; if not, keep build_day_universe set
        missing = [d for d in preferred if d not in day_data]
        if not missing and len(preferred) >= n_days:
            dates = preferred
            day_data = {d: day_data[d] for d in dates}
            date_sources = {d: date_sources.get(d, sibling["date_sources"].get(d, "unknown")) for d in dates}
        else:
            notes.append(
                f"Sibling dates incomplete in local day_data (missing={missing}); "
                f"using build_day_universe dates instead."
            )
    else:
        notes.append(
            "Sibling JSON not ready — using compare_macd_vs_williams_early_20d.build_day_universe."
        )

    for day in dates:
        _write_replay_cache(day, day_data[day])

    return {
        "days": dates,
        "day_sources": {d: date_sources.get(d, "unknown") for d in dates},
        "notes": notes,
    }


def _cost_gross_ratio(trades: list[Trade]) -> Optional[float]:
    if not trades:
        return None
    total_cost = sum(float(t.cost) for t in trades)
    gross_abs = sum(abs(float(t.gross_pnl)) for t in trades)
    if gross_abs <= 0:
        return None
    return round(total_cost / gross_abs * 100.0, 3)


def _reentry_metrics(trades: list[Trade]) -> dict[str, Any]:
    re_trades = [t for t in trades if t.entry_kind == ENTRY_CONTINUATION]
    if not re_trades:
        return {
            "count": 0,
            "net_pnl": 0.0,
            "win_rate_pct": None,
            "profit_factor": None,
            "cost_gross_pct": None,
        }
    m = _metrics(re_trades)
    nets = [t.net_pnl for t in re_trades]
    wins = [n for n in nets if n > 0]
    return {
        "count": len(re_trades),
        "net_pnl": round(sum(nets), 2),
        "win_rate_pct": round(len(wins) / len(nets) * 100.0, 2),
        "profit_factor": m["pf"],
        "cost_gross_pct": _cost_gross_ratio(re_trades),
    }


def summarize(vr: VariantResult) -> dict[str, Any]:
    m = _metrics(vr.trades)
    initial = [t for t in vr.trades if t.entry_kind == ENTRY_INITIAL]
    reentry = _reentry_metrics(vr.trades)
    by_day: dict[str, Any] = {}
    for day in vr.days:
        day_trades = [t for t in vr.trades if t.day == day]
        by_day[day] = {
            **_metrics(day_trades),
            "round_trips": len(day_trades),
            "initial_pnl": round(
                sum(t.net_pnl for t in day_trades if t.entry_kind == ENTRY_INITIAL), 2
            ),
            "reentry_pnl": round(
                sum(t.net_pnl for t in day_trades if t.entry_kind == ENTRY_CONTINUATION), 2
            ),
        }
    return {
        "variant": vr.variant,
        "round_trips": len(vr.trades),
        "initial_trades": len(initial),
        "reentry": reentry,
        "initial_pnl": round(sum(t.net_pnl for t in initial), 2),
        "net_pnl": m["net"],
        "ret_pct": m["ret"],
        "profit_factor": m["pf"],
        "mdd_pct": m["mdd"],
        "win_rate_pct": m["wr"],
        "cost_gross_pct": _cost_gross_ratio(vr.trades),
        "by_day": by_day,
        "bad_reentries": vr.bad_reentries,
        "trades": [asdict(t) for t in vr.trades],
    }


def evaluate_gates(off: dict[str, Any], on: dict[str, Any]) -> dict[str, Any]:
    re = on.get("reentry") or {}
    mdd_delta = float(on["mdd_pct"] or 0) - float(off["mdd_pct"] or 0)
    off_cg = off.get("cost_gross_pct")
    on_cg = on.get("cost_gross_pct")
    if off_cg is None or on_cg is None:
        cg_delta = None
        cg_ok = False
        cg_note = "missing cost/gross"
    else:
        cg_delta = float(on_cg) - float(off_cg)
        cg_ok = cg_delta <= 5.0
        cg_note = f"Δ={cg_delta:.3f}pp"

    re_pf = re.get("profit_factor")
    re_wr = re.get("win_rate_pct")
    re_count = int(re.get("count") or 0)
    re_pf_ok = (re_pf is not None) and (float(re_pf) >= 1.3) and re_count > 0
    re_wr_ok = (re_wr is not None) and (float(re_wr) >= 55.0) and re_count > 0

    gates = {
        "net_pnl_increases": {
            "pass": float(on["net_pnl"]) > float(off["net_pnl"]),
            "off": off["net_pnl"],
            "on": on["net_pnl"],
            "detail": f"ON({on['net_pnl']}) > OFF({off['net_pnl']})",
        },
        "pf_does_not_decrease": {
            "pass": float(on["profit_factor"]) >= float(off["profit_factor"]),
            "off": off["profit_factor"],
            "on": on["profit_factor"],
            "detail": f"ON PF {on['profit_factor']} ≥ OFF PF {off['profit_factor']}",
        },
        "mdd_increase_le_0_2pp": {
            "pass": mdd_delta <= 0.2,
            "off": off["mdd_pct"],
            "on": on["mdd_pct"],
            "delta_pp": round(mdd_delta, 3),
            "detail": f"MDD Δ={mdd_delta:.3f}pp ≤ 0.2",
        },
        "reentry_pf_ge_1_3": {
            "pass": re_pf_ok,
            "value": re_pf,
            "count": re_count,
            "detail": f"reentry PF={re_pf} (need ≥1.3, n={re_count})",
        },
        "reentry_win_rate_ge_55": {
            "pass": re_wr_ok,
            "value": re_wr,
            "count": re_count,
            "detail": f"reentry WR={re_wr}% (need ≥55, n={re_count})",
        },
        "cost_gross_worsening_le_5pp": {
            "pass": cg_ok,
            "off": off_cg,
            "on": on_cg,
            "delta_pp": cg_delta,
            "detail": f"cost/gross {cg_note} (need Δ≤5pp)",
        },
    }
    all_pass = all(bool(g["pass"]) for g in gates.values())
    return {
        "gates": gates,
        "all_pass": all_pass,
        "verdict": "ADOPT_RECOMMENDED" if all_pass else "DO_NOT_ADOPT",
    }


def render_md(report: dict[str, Any]) -> str:
    off = report["OFF"]
    on = report["ON"]
    gates = report["adoption"]["gates"]
    cg_delta = report["delta_on_minus_off"].get("cost_gross_pct")
    lines = [
        "# MACD B + TP/SL — Continuation Re-entry OFF vs ON (≥20d)",
        "",
        f"- Generated: `{report['generated_at']}`",
        f"- Days ({len(report['days'])}): {', '.join(report['days'])}",
        f"- Data notes: {'; '.join(report.get('data_notes') or [])}",
        f"- Live `CONTINUATION_REENTRY_ENABLED` remains: "
        f"**{CONTINUATION_REENTRY_ENABLED}** "
        f"(confirmed unchanged; `live_flag_still_false="
        f"{report['live_flag_still_false']}`)",
        f"- Fill: next 1m open + {ADVERSE_PCT}% adverse + TradeCostEngine; "
        f"base delay={DELAY_MIN}m; TP={TP_NET_PCT}% / SL={SL_NET_PCT}%",
        "",
        "## Day sources",
        "",
        "| Day | Source |",
        "|-----|--------|",
    ]
    for d in report["days"]:
        lines.append(f"| {d} | {report['day_sources'].get(d, '?')} |")

    lines += [
        "",
        "## OFF vs ON summary",
        "",
        "| Metric | OFF | ON | Δ |",
        "|--------|-----|----|---|",
        f"| Round-trips | {off['round_trips']} | {on['round_trips']} | "
        f"{on['round_trips'] - off['round_trips']} |",
        f"| Net PnL | {off['net_pnl']:,.0f} | {on['net_pnl']:,.0f} | "
        f"{on['net_pnl'] - off['net_pnl']:,.0f} |",
        f"| Return % | {off['ret_pct']} | {on['ret_pct']} | "
        f"{round(on['ret_pct'] - off['ret_pct'], 3)} |",
        f"| Profit Factor | {off['profit_factor']} | {on['profit_factor']} | "
        f"{round(on['profit_factor'] - off['profit_factor'], 3)} |",
        f"| MDD % | {off['mdd_pct']} | {on['mdd_pct']} | "
        f"{round(on['mdd_pct'] - off['mdd_pct'], 3)} |",
        f"| Win rate % | {off['win_rate_pct']} | {on['win_rate_pct']} | "
        f"{round(on['win_rate_pct'] - off['win_rate_pct'], 2)} |",
        f"| Cost/Gross % | {off['cost_gross_pct']} | {on['cost_gross_pct']} | {cg_delta} |",
        f"| Re-entry count | 0 | {on['reentry']['count']} | {on['reentry']['count']} |",
        f"| Re-entry Net PnL | — | {on['reentry']['net_pnl']:,.0f} | — |",
        f"| Re-entry WR % | — | {on['reentry']['win_rate_pct']} | — |",
        f"| Re-entry PF | — | {on['reentry']['profit_factor']} | — |",
        "",
        "## Adoption gates",
        "",
        "| Gate | Pass? | Detail |",
        "|------|-------|--------|",
    ]
    for name, g in gates.items():
        lines.append(f"| {name} | {'PASS' if g['pass'] else 'FAIL'} | {g['detail']} |")
    lines += [
        "",
        f"**Verdict: `{report['adoption']['verdict']}`** "
        "(live flag stays False regardless).",
        "",
        "## Stress (+1m fill delay)",
        "",
    ]
    stress = report.get("stress_delay_plus1m") or {}
    if stress:
        s_off = stress.get("OFF") or {}
        s_on = stress.get("ON") or {}
        lines += [
            f"- OFF Net={s_off.get('net_pnl')} PF={s_off.get('profit_factor')} "
            f"MDD={s_off.get('mdd_pct')} RT={s_off.get('round_trips')}",
            f"- ON  Net={s_on.get('net_pnl')} PF={s_on.get('profit_factor')} "
            f"MDD={s_on.get('mdd_pct')} RT={s_on.get('round_trips')} "
            f"reN={((s_on.get('reentry') or {}).get('count'))}",
            "",
        ]
    lines += [
        "## Live flag confirmation",
        "",
        f"- `CONTINUATION_REENTRY_ENABLED` = `{CONTINUATION_REENTRY_ENABLED}`",
        "- Continuation re-entry code remains present "
        "(`evaluate_continuation_reentry`).",
        "",
        "## Artifacts",
        "",
        f"- `{OUT_JSON.as_posix()}`",
        f"- `{OUT_MD.as_posix()}`",
        "",
    ]
    return "\n".join(lines)


def main() -> int:
    print("=" * 72)
    print("MACD B TP/SL — CONTINUATION_REENTRY OFF vs ON (≥20 trading days)")
    print(f"Live flag CONTINUATION_REENTRY_ENABLED={CONTINUATION_REENTRY_ENABLED} (unchanged)")
    print("=" * 72)

    ds = ensure_dataset(MIN_DAYS)
    days = ds["days"]
    print(f"\nUsing {len(days)} days:")
    for d in days:
        print(f"  {d}  [{ds['day_sources'].get(d)}]")
    for n in ds.get("notes") or []:
        print(f"  note: {n}")

    print("\nReplaying OFF …")
    off_vr = replay_variant(
        days, allow_continuation=False, variant="OFF_NO_REENTRY", delay_min=DELAY_MIN
    )
    print("Replaying ON …")
    on_vr = replay_variant(
        days, allow_continuation=True, variant="ON_REENTRY", delay_min=DELAY_MIN
    )
    off = summarize(off_vr)
    on = summarize(on_vr)
    adoption = evaluate_gates(off, on)

    print("\nStress +1m delay …")
    s_off = summarize(
        replay_variant(
            days, allow_continuation=False, variant="OFF_DELAY2", delay_min=DELAY_MIN + 1
        )
    )
    s_on = summarize(
        replay_variant(
            days, allow_continuation=True, variant="ON_DELAY2", delay_min=DELAY_MIN + 1
        )
    )
    for side in (s_off, s_on):
        side.pop("trades", None)
        side.pop("by_day", None)
    stress = {"OFF": s_off, "ON": s_on}

    report = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "strategy": "MACD_B_TPSL",
        "comparison": "CONTINUATION_REENTRY_OFF_vs_ON",
        "days": days,
        "n_days": len(days),
        "day_sources": ds["day_sources"],
        "data_notes": ds.get("notes") or [],
        "fill_model": {
            "delay_min": DELAY_MIN,
            "adverse_pct": ADVERSE_PCT,
            "tp_net_pct": TP_NET_PCT,
            "sl_net_pct": SL_NET_PCT,
            "initial_cash": INITIAL_CASH,
            "entry_window": "09:00-14:55",
            "flatten": "15:00",
        },
        "OFF": off,
        "ON": on,
        "delta_on_minus_off": {
            "net_pnl": round(on["net_pnl"] - off["net_pnl"], 2),
            "profit_factor": round(on["profit_factor"] - off["profit_factor"], 3),
            "mdd_pct": round(on["mdd_pct"] - off["mdd_pct"], 3),
            "round_trips": on["round_trips"] - off["round_trips"],
            "cost_gross_pct": (
                None
                if off["cost_gross_pct"] is None or on["cost_gross_pct"] is None
                else round(on["cost_gross_pct"] - off["cost_gross_pct"], 3)
            ),
        },
        "adoption": adoption,
        "stress_delay_plus1m": stress,
        "live_flag_still_false": CONTINUATION_REENTRY_ENABLED is False,
        "live_CONTINUATION_REENTRY_ENABLED": CONTINUATION_REENTRY_ENABLED,
        "note": (
            "ADOPT_RECOMMENDED means recommendation only; "
            "do not flip live CONTINUATION_REENTRY_ENABLED unless explicitly requested."
        ),
    }

    STATE.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    OUT_MD.write_text(render_md(report), encoding="utf-8")

    print("\n## Summary")
    print(
        f"OFF: RT={off['round_trips']} Net={off['net_pnl']:,.0f} "
        f"PF={off['profit_factor']} MDD={off['mdd_pct']}% CG={off['cost_gross_pct']}"
    )
    print(
        f"ON : RT={on['round_trips']} Net={on['net_pnl']:,.0f} "
        f"PF={on['profit_factor']} MDD={on['mdd_pct']}% CG={on['cost_gross_pct']} "
        f"reN={on['reentry']['count']} rePnL={on['reentry']['net_pnl']:,.0f} "
        f"reWR={on['reentry']['win_rate_pct']} rePF={on['reentry']['profit_factor']}"
    )
    print(f"\n>>> VERDICT: {adoption['verdict']}")
    for name, g in adoption["gates"].items():
        print(f"  [{'PASS' if g['pass'] else 'FAIL'}] {name}: {g['detail']}")
    print(f"Live CONTINUATION_REENTRY_ENABLED still {CONTINUATION_REENTRY_ENABLED}")
    print(f"Wrote {OUT_JSON}")
    print(f"Wrote {OUT_MD}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
