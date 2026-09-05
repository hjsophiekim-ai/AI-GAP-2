"""raw.json(_tmp_20260904_slot23_tq_fullchain.py 산출물) 기반 리포트 집계.
READ-ONLY, production 무관. 결과는 report.json + report.txt(UTF-8)로 저장한다
(콘솔 cp949 인코딩 문제 회피)."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = PROJECT_ROOT / "data" / "validation" / "slot23_tq_fullchain"
RAW = BASE / "raw.json"
OUT_JSON = BASE / "report.json"
OUT_TXT = BASE / "report.txt"

VARIANTS = ["A", "B", "C", "D"]
LABEL = {
    "A": "A 현행 (TW2 3-SLOT + 조기익절)",
    "B": "B Slot2/3 TQ>=3/5",
    "C": "C Slot2 TQ>=3/5 + Slot3 TQ>=4/5",
    "D": "D B + 슬롯 보존",
}


def metrics(trades: list[dict], n_days: int) -> dict:
    if not trades:
        return {}
    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    n = len(df)
    wins = df[df["net_pct"] > 0]
    losses = df[df["net_pct"] < 0]
    simple_cum = df["net_pct"].sum()
    equity = (1 + df["net_pct"] / 100).cumprod()
    compound_cum = (equity.iloc[-1] - 1) * 100
    gross_profit = wins["net_pct"].sum()
    gross_loss = -losses["net_pct"].sum()
    pf = (gross_profit / gross_loss) if gross_loss > 0 else float("inf")
    drawdown = (equity / equity.cummax() - 1) * 100
    daily = df.groupby("date")["net_pct"].sum()
    top10 = df.nlargest(min(10, n), "net_pct")
    rest = df.drop(top10.index)
    if len(rest):
        rest_sorted = rest.sort_values("exit_time")
        top10_excl_compound = ((1 + rest_sorted["net_pct"] / 100).cumprod().iloc[-1] - 1) * 100
    else:
        top10_excl_compound = 0.0
    streak = max_streak = 0
    cur_pnl = max_streak_pnl = 0.0
    for v in df["net_pct"]:
        if v < 0:
            streak += 1
            cur_pnl += v
        else:
            streak = 0
            cur_pnl = 0.0
        if streak > max_streak:
            max_streak, max_streak_pnl = streak, cur_pnl
    return {
        "trades": n,
        "avg_trades_per_day": round(n / n_days, 3),
        "wins": int(len(wins)), "losses": int(len(losses)),
        "breakeven": int((df["net_pct"] == 0).sum()),
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "simple_cum_return_pct": round(float(simple_cum), 4),
        "compound_cum_return_pct": round(float(compound_cum), 4),
        "avg_return_per_trade_pct": round(float(df["net_pct"].mean()), 4),
        "profit_factor": round(float(pf), 4) if pf != float("inf") else None,
        "mdd_pct": round(float(drawdown.min()), 4),
        "profit_days": int((daily > 0).sum()),
        "loss_days": int((daily < 0).sum()),
        "flat_days": int((daily == 0).sum()),
        "max_consecutive_losses_trades": int(max_streak),
        "max_consecutive_losses_cum_pct": round(float(max_streak_pnl), 4),
        "top10_excluded_simple_cum_pct": round(float(rest["net_pct"].sum()), 4),
        "top10_excluded_compound_cum_pct": round(float(top10_excl_compound), 4),
    }


def key_of(t: dict) -> tuple:
    return (t["decision_idx"], t["direction"])


def find_trade(trades: list[dict], decision_idx: int, direction: str):
    for t in trades:
        if t["decision_idx"] == decision_idx and t["direction"] == direction:
            return t
    return None


def hhmm(iso: str | None) -> str:
    return iso[11:16] if iso else "-"


def brief(t: dict) -> str:
    return (f"{hhmm(t['entry_time'])}->{hhmm(t['exit_time'])} S{t['slot_number']} "
            f"#{t['flag_ordinal']} {t['direction'][:4]} {t['net_pct']:+.2f}% "
            f"({t['exit_reason']})")


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    dates = raw["dates"]
    n_days = len(dates)
    tv = {v: raw[v] for v in VARIANTS}

    L: list[str] = []
    add = L.append

    add(f"기간: {dates[0]} ~ {dates[-1]}  ({n_days}영업일)")
    add(f"B == D 완전동일: {raw['B_equals_D']}")
    add("")

    # ── 0) 종합 지표 ────────────────────────────────────────────────
    met = {v: metrics(tv[v], n_days) for v in VARIANTS}
    add("=" * 100)
    add("[0] 종합 지표 (최근 30영업일)")
    add("=" * 100)
    rows = [
        ("거래수", "trades", "{:.0f}"),
        ("일평균 거래수", "avg_trades_per_day", "{:.3f}"),
        ("승/패/보합", None, None),
        ("승률(%)", "win_rate_pct", "{:.2f}"),
        ("단순합계 수익(%)", "simple_cum_return_pct", "{:+.3f}"),
        ("복리 수익(%)", "compound_cum_return_pct", "{:+.3f}"),
        ("평균수익/거래(%)", "avg_return_per_trade_pct", "{:+.4f}"),
        ("Profit Factor", "profit_factor", "{:.4f}"),
        ("MDD(%)", "mdd_pct", "{:.3f}"),
        ("Top10 제외 단순(%)", "top10_excluded_simple_cum_pct", "{:+.3f}"),
        ("Top10 제외 복리(%)", "top10_excluded_compound_cum_pct", "{:+.3f}"),
        ("수익일", "profit_days", "{:.0f}"),
        ("손실일", "loss_days", "{:.0f}"),
        ("무거래/보합일", "flat_days", "{:.0f}"),
        ("최대연속손실(건)", "max_consecutive_losses_trades", "{:.0f}"),
        ("최대연속손실 누적(%)", "max_consecutive_losses_cum_pct", "{:+.3f}"),
    ]
    add(f"{'지표':<24}" + "".join(f"{v:>18}" for v in VARIANTS))
    add("-" * 100)
    for name, k, fmt in rows:
        if k is None:
            vals = [f"{met[v]['wins']}/{met[v]['losses']}/{met[v]['breakeven']}" for v in VARIANTS]
        else:
            vals = []
            for v in VARIANTS:
                x = met[v].get(k)
                vals.append("n/a" if x is None else fmt.format(x))
        add(f"{name:<24}" + "".join(f"{s:>18}" for s in vals))
    add("")
    for v in VARIANTS:
        add(f"  {v} = {LABEL[v]}")
    add("")

    # ── 1)/2) Slot2 / Slot3 차단 거래 + A 기준 사후손익 ──────────────
    blocked_report: dict = {}
    for v in ("B", "C", "D"):
        blocked_report[v] = []
        for blk in raw[f"blocks_{v}"]:
            a_t = find_trade(tv["A"], blk["decision_idx"], blk["direction"])
            blocked_report[v].append({
                **blk,
                "A_entered": a_t is not None,
                "A_entry_time": a_t["entry_time"] if a_t else None,
                "A_exit_time": a_t["exit_time"] if a_t else None,
                "A_exit_reason": a_t["exit_reason"] if a_t else None,
                "A_net_pct": a_t["net_pct"] if a_t else None,
                "A_peak_net_pct": a_t["peak_net_pct"] if a_t else None,
                "A_slot_number": a_t["slot_number"] if a_t else None,
            })

    for slot_n, title in ((2, "[1] Slot2 에서 차단된 후보 전부 + A 기준 사후손익"),
                          (3, "[2] Slot3 에서 차단된 후보 전부 + A 기준 사후손익")):
        add("=" * 100)
        add(title)
        add("=" * 100)
        for v in ("B", "C"):
            items = [b for b in blocked_report[v] if b["slot_number"] == slot_n]
            add(f"-- {v} ({LABEL[v]}) : {len(items)}건")
            if not items:
                add("   (없음)")
            for b in items:
                add(f"   {b['date']} {hhmm(b['decision_at'])} {b['direction']:<10} "
                    f"세션={b['session']:<9} 그날 {b['flag_ordinal']}번째 후보  "
                    f"TQ {b['tq_passed']}/5 (요구 {b['tq_required']})")
                if b["A_entered"]:
                    add(f"      -> A에서는 진입: {hhmm(b['A_entry_time'])}->{hhmm(b['A_exit_time'])} "
                        f"Slot{b['A_slot_number']} 실현 {b['A_net_pct']:+.3f}% "
                        f"(MFE {b['A_peak_net_pct']:+.3f}%, {b['A_exit_reason']})")
                else:
                    add("      -> A에는 동일 후보의 체결 기록 없음 (체인 분기 이후 발생)")
            sub = [b["A_net_pct"] for b in items if b["A_net_pct"] is not None]
            if sub:
                add(f"   A기준 차단분 합계 {sum(sub):+.3f}%  (승 {sum(1 for x in sub if x>0)} / "
                    f"패 {sum(1 for x in sub if x<0)})")
            add("")

    # ── 3) 차단 후 새로 진입한 거래 (원래 4·5번째였던 플래그 포함) ────
    add("=" * 100)
    add("[3] 차단 덕분에 남은 슬롯으로 새로 진입한 거래 (A에는 없던 거래)")
    add("=" * 100)
    new_entries: dict = {}
    for v in ("B", "C", "D"):
        a_keys = {key_of(t) for t in tv["A"]}
        news = [t for t in tv[v] if key_of(t) not in a_keys]
        new_entries[v] = news
        add(f"-- {v} ({LABEL[v]}) : {len(news)}건")
        if not news:
            add("   (없음)")
        for t in news:
            add(f"   {t['date']} {hhmm(t['entry_time'])}->{hhmm(t['exit_time'])} "
                f"Slot{t['slot_number']} 그날 {t['flag_ordinal']}번째 후보 {t['direction']:<10} "
                f"실현 {t['net_pct']:+.3f}% (MFE {t['peak_net_pct']:+.3f}%, {t['exit_reason']})")
        if news:
            add(f"   신규진입 합계 {sum(t['net_pct'] for t in news):+.3f}%  "
                f"(승 {sum(1 for t in news if t['net_pct']>0)} / "
                f"패 {sum(1 for t in news if t['net_pct']<0)})")
            ord4 = [t for t in news if t["flag_ordinal"] >= 4]
            add(f"   그중 그날 4번째 이상 플래그였던 것: {len(ord4)}건, "
                f"합계 {sum(t['net_pct'] for t in ord4):+.3f}%")
        add("")

    # A에는 있는데 사라진 거래(차단 외 체인효과 포함)
    add("-- 참고: A에는 있었으나 각 안에서 사라진 거래")
    lost: dict = {}
    for v in ("B", "C", "D"):
        v_keys = {key_of(t) for t in tv[v]}
        gone = [t for t in tv["A"] if key_of(t) not in v_keys]
        lost[v] = gone
        add(f"   {v}: {len(gone)}건, A기준 합계 {sum(t['net_pct'] for t in gone):+.3f}%")
    add("")

    # ── 4) 좋아진 날 / 나빠진 날 ─────────────────────────────────────
    add("=" * 100)
    add("[4] 날짜별 손익 변화 (각 안 - A)")
    add("=" * 100)
    daily: dict = {}
    for v in VARIANTS:
        s = pd.DataFrame(tv[v]).groupby("date")["net_pct"].sum() if tv[v] else pd.Series(dtype=float)
        daily[v] = {d: float(s.get(d, 0.0)) for d in dates}
    add(f"{'date':<10}" + "".join(f"{v:>12}" for v in VARIANTS)
        + f"{'B-A':>10}{'C-A':>10}{'D-A':>10}")
    add("-" * 78)
    for d in dates:
        marks = ""
        for v in ("B", "C", "D"):
            marks += f"{daily[v][d] - daily['A'][d]:>+10.3f}"
        add(f"{d:<10}" + "".join(f"{daily[v][d]:>+12.3f}" for v in VARIANTS) + marks)
    add("")
    day_changes: dict = {}
    for v in ("B", "C", "D"):
        better = [(d, daily[v][d] - daily["A"][d]) for d in dates if daily[v][d] - daily["A"][d] > 1e-9]
        worse = [(d, daily[v][d] - daily["A"][d]) for d in dates if daily[v][d] - daily["A"][d] < -1e-9]
        day_changes[v] = {"better": better, "worse": worse}
        add(f"-- {v}: 좋아진 날 {len(better)}일 (합 {sum(x for _, x in better):+.3f}%) / "
            f"나빠진 날 {len(worse)}일 (합 {sum(x for _, x in worse):+.3f}%) / "
            f"동일 {n_days - len(better) - len(worse)}일")
        for d, x in sorted(better, key=lambda z: -z[1]):
            add(f"      좋아짐 {d} {x:+.3f}%")
        for d, x in sorted(worse, key=lambda z: z[1]):
            add(f"      나빠짐 {d} {x:+.3f}%")
        add("")

    # ── 5) 놓친 +3% 이상 러너 ────────────────────────────────────────
    add("=" * 100)
    add("[5] 차단 때문에 놓친 +3% 이상 러너 (A 기준 MFE 또는 실현이 +3% 이상)")
    add("=" * 100)
    runners: dict = {}
    for v in ("B", "C", "D"):
        rs = [b for b in blocked_report[v]
              if b["A_net_pct"] is not None
              and (b["A_peak_net_pct"] >= 3.0 or b["A_net_pct"] >= 3.0)]
        runners[v] = rs
        add(f"-- {v}: {len(rs)}건")
        if not rs:
            add("   (없음)")
        for b in rs:
            add(f"   {b['date']} {hhmm(b['decision_at'])} Slot{b['slot_number']} {b['direction']:<10} "
                f"TQ {b['tq_passed']}/5 -> A실현 {b['A_net_pct']:+.3f}% "
                f"(MFE {b['A_peak_net_pct']:+.3f}%, {b['A_exit_reason']})")
        add("")
    # 참고: 각 안에서 사라진 거래 중 +3% 러너 (차단 외 체인효과 포함)
    add("-- 참고: 차단 외 체인효과로 사라진 것까지 포함한 +3% 러너 상실")
    for v in ("B", "C", "D"):
        rs = [t for t in lost[v] if t["peak_net_pct"] >= 3.0 or t["net_pct"] >= 3.0]
        add(f"   {v}: {len(rs)}건 " + (", ".join(
            f"{t['date']} {hhmm(t['entry_time'])} {t['net_pct']:+.2f}%(MFE {t['peak_net_pct']:+.2f}%)"
            for t in rs) or "(없음)"))
    add("")

    # ── 6) 최근 5영업일 타임라인 ────────────────────────────────────
    add("=" * 100)
    add("[6] 최근 5영업일 날짜별 거래 타임라인")
    add("=" * 100)
    timeline: dict = {}
    for d in dates[-5:]:
        add(f"### {d}")
        timeline[d] = {}
        for v in VARIANTS:
            ts = [t for t in tv[v] if t["date"] == d]
            timeline[d][v] = ts
            tot = sum(t["net_pct"] for t in ts)
            add(f"  {v}: {len(ts)}건 합계 {tot:+.3f}%")
            for t in ts:
                add(f"      {brief(t)}")
            blks = [b for b in raw.get(f"blocks_{v}", []) if b["date"] == d]
            for b in blks:
                add(f"      [차단] {hhmm(b['decision_at'])} Slot{b['slot_number']} "
                    f"#{b['flag_ordinal']} {b['direction'][:4]} TQ {b['tq_passed']}/5"
                    f"<{b['tq_required']}")
        add("")

    # ── 7) 2026-09-04 로직 트레이스 ─────────────────────────────────
    add("=" * 100)
    add("[7] 2026-09-04 신호 로직 트레이스")
    add("=" * 100)
    add("당일 확정 MACD zero-cross 플래그 (봉 시각 / T+3 확정판정 시각):")
    for f in raw["trace_day_flags"]:
        add(f"   flag_bar {hhmm(f['flag_bar_at'])}  ->  T+3 판정 {hhmm(f['confirm_at'])}  {f['direction']}")
    add("")
    for v in VARIANTS:
        add(f"--- {v} ({LABEL[v]}) ---")
        for tr in raw["trace"][v]:
            add(f"  후보#{tr['flag_ordinal']}  flag {hhmm(tr['flag_bar_at'])} / 판정 "
                f"{hhmm(tr['decision_at'])}  {tr['direction']}")
            add(f"      진입전 상태: slots_used={tr['slots_used_before']} "
                f"morning={tr['morning_count_before']} afternoon={tr['afternoon_count_before']} "
                f"보유={tr['position_before'] or '없음'}")
            add(f"      TW2 기본: approved={tr.get('tw2_approved')} reason={tr.get('tw2_block_reason')}")
            if "tw2_extra_veto" in tr:
                add(f"      TW2 추가veto: {tr['tw2_extra_veto']} {tr.get('tw2_extra_veto_reason') or ''}")
            if "slot_allowed" in tr:
                add(f"      slot: allowed={tr['slot_allowed']} slot_number={tr['slot_number']} "
                    f"session={tr['session']} quality_gate={tr['requires_quality_gate']} "
                    f"teg_gate={tr['requires_teg_gate']} reject={tr['slot_reject_reason']}")
            if "prod_quality_approved" in tr:
                add(f"      production TQ: {tr['prod_quality_passed']}/5 "
                    f"(요구 {tr['prod_quality_required']}) approved={tr['prod_quality_approved']}")
            if "teg_approved" in tr:
                add(f"      TEGv2: approved={tr['teg_approved']} {tr.get('teg_block_reason') or ''}")
            if tr.get("extra_tq_required") is not None:
                add(f"      추가 TQ 게이트(Slot{tr['slot_number']}, 요구 {tr['extra_tq_required']}/5): "
                    f"{tr.get('extra_tq_passed')}/5 approved={tr.get('extra_tq_approved')}")
                if tr.get("extra_tq_conditions"):
                    add("        조건: " + ", ".join(
                        f"{k}={'O' if x else 'X'}" for k, x in tr["extra_tq_conditions"].items()))
            add(f"      => 최종 approved={tr['final_approved']} reason={tr['final_reason']}")
        ts = [t for t in tv[v] if t["date"] in ("20260904",)]
        add(f"    당일 실제 진입 {len(ts)}건 합계 {sum(t['net_pct'] for t in ts):+.3f}%")
        for t in ts:
            add(f"      {brief(t)}")
        add("")

    OUT_TXT.write_text("\n".join(L), encoding="utf-8")
    OUT_JSON.write_text(json.dumps({
        "period": {"start": dates[0], "end": dates[-1], "n_days": n_days},
        "metrics": met,
        "blocked": blocked_report,
        "new_entries": new_entries,
        "lost_vs_A": lost,
        "daily": daily,
        "day_changes": day_changes,
        "runners_missed": runners,
        "timeline_last5": timeline,
        "trace_20260904": raw["trace"],
        "B_equals_D": raw["B_equals_D"],
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUT_TXT}")
    print(f"saved {OUT_JSON}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
