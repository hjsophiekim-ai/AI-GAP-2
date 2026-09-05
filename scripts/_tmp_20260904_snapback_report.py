"""raw.json(_tmp_20260904_snapback_fullchain.py 산출물) 기반 리포트.
READ-ONLY, production 무관. report.txt(UTF-8) + report.json 저장."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = PROJECT_ROOT / "data" / "validation" / "snapback"
RAW = BASE / "raw.json"

VARIANTS = ["A", "B", "B_TW2KEEP"]
LABEL = {
    "A": "A 현행 (TW2 3-SLOT + 조기익절)",
    "B": "B A + SNAPBACK 추가 1회",
    "B_TW2KEEP": "B_TW2KEEP [민감도] SNAPBACK 에도 TW2 시간창 이전단계 요구",
}
SYMBOL_NAME = {"0193T0": "LONG", "0197X0": "INVERSE"}


def metrics(trades: list[dict], dates: list) -> dict:
    ts = [t for t in trades if t["date"] in set(dates)]
    if not ts:
        return {}
    df = pd.DataFrame(ts).sort_values("exit_time").reset_index(drop=True)
    n = len(df)
    wins = df[df["net_pct"] > 0]
    losses = df[df["net_pct"] < 0]
    eq = (1 + df["net_pct"] / 100).cumprod()
    gp = wins["net_pct"].sum()
    gl = -losses["net_pct"].sum()
    dd = (eq / eq.cummax() - 1) * 100
    daily = df.groupby("date")["net_pct"].sum()
    top10 = df.nlargest(min(10, n), "net_pct")
    rest = df.drop(top10.index)
    if len(rest):
        rs = rest.sort_values("exit_time")
        t10c = ((1 + rs["net_pct"] / 100).cumprod().iloc[-1] - 1) * 100
    else:
        t10c = 0.0
    streak = mx = 0
    cur = mxp = 0.0
    for v in df["net_pct"]:
        if v < 0:
            streak += 1
            cur += v
        else:
            streak = 0
            cur = 0.0
        if streak > mx:
            mx, mxp = streak, cur
    return {
        "trades": n,
        "snapback_trades": int(df.get("is_snapback", pd.Series([False] * n)).sum()),
        "avg_trades_per_day": round(n / len(dates), 3),
        "wins": int(len(wins)), "losses": int(len(losses)),
        "win_rate_pct": round(len(wins) / n * 100, 2),
        "simple_pct": round(float(df["net_pct"].sum()), 4),
        "compound_pct": round(float((eq.iloc[-1] - 1) * 100), 4),
        "avg_pct": round(float(df["net_pct"].mean()), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "mdd_pct": round(float(dd.min()), 4),
        "top10_excl_simple_pct": round(float(rest["net_pct"].sum()), 4),
        "top10_excl_compound_pct": round(float(t10c), 4),
        "profit_days": int((daily > 0).sum()), "loss_days": int((daily < 0).sum()),
        "max_consec_losses": int(mx), "max_consec_losses_pct": round(float(mxp), 4),
        "trades_ge_3pct": int((df["net_pct"] >= 3.0).sum()),
        "trades_ge_5pct": int((df["net_pct"] >= 5.0).sum()),
    }


def sub(trades: list[dict]) -> dict:
    if not trades:
        return {"trades": 0}
    df = pd.DataFrame(trades)
    w = df[df["net_pct"] > 0]
    l = df[df["net_pct"] < 0]
    gp = w["net_pct"].sum()
    gl = -l["net_pct"].sum()
    return {
        "trades": len(df), "wins": int(len(w)), "losses": int(len(l)),
        "win_rate_pct": round(len(w) / len(df) * 100, 2),
        "avg_pct": round(float(df["net_pct"].mean()), 4),
        "total_pct": round(float(df["net_pct"].sum()), 4),
        "pf": round(float(gp / gl), 4) if gl > 0 else None,
        "gross_profit_pct": round(float(gp), 4), "gross_loss_pct": round(float(-gl), 4),
        "best_pct": round(float(df["net_pct"].max()), 4),
        "worst_pct": round(float(df["net_pct"].min()), 4),
        "trades_ge_3pct": int((df["net_pct"] >= 3.0).sum()),
        "trades_ge_5pct": int((df["net_pct"] >= 5.0).sum()),
    }


def hhmm(x):
    return x[11:16] if x else "-"


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    d30, d60 = raw["dates30"], raw["dates60"]
    tv = {v: raw[v] for v in VARIANTS}
    L: list[str] = []
    add = L.append

    th = raw["thresholds"]
    add(f"평가창 30일: {d30[0]} ~ {d30[-1]}   (참고 60일: {d60[0]} ~ {d60[-1]})")
    add(f"SNAPBACK 고정 임계값: 창 {th['window'][0]}~{th['window'][1]}, "
        f"day_extreme<={th['THR_EXTREME']}, vwap<={th['THR_VWAP']}, "
        f"slope<={th['THR_SLOPE']}, C4=T+3유지, 4개 중 {th['MIN_CONDITIONS']}개 이상")
    add("")

    met30 = {v: metrics(tv[v], d30) for v in VARIANTS}
    met60 = {v: metrics(tv[v], d60) for v in VARIANTS}

    rows = [
        ("거래수", "trades", "{:.0f}"),
        ("  그중 SNAPBACK", "snapback_trades", "{:.0f}"),
        ("일평균 거래수", "avg_trades_per_day", "{:.3f}"),
        ("승/패", None, None),
        ("승률(%)", "win_rate_pct", "{:.2f}"),
        ("단순합계 수익(%)", "simple_pct", "{:+.3f}"),
        ("복리 수익(%)", "compound_pct", "{:+.3f}"),
        ("평균수익/거래(%)", "avg_pct", "{:+.4f}"),
        ("Profit Factor", "pf", "{:.4f}"),
        ("MDD(%)", "mdd_pct", "{:.3f}"),
        ("Top10 제외 단순(%)", "top10_excl_simple_pct", "{:+.3f}"),
        ("Top10 제외 복리(%)", "top10_excl_compound_pct", "{:+.3f}"),
        ("수익일/손실일", None, "days"),
        ("최대연속손실(건/누적%)", None, "streak"),
        ("+3% 이상 거래", "trades_ge_3pct", "{:.0f}"),
        ("+5% 이상 거래", "trades_ge_5pct", "{:.0f}"),
    ]

    for title, met, dates in (("[1] 종합 지표 — 30영업일 (판정 기준)", met30, d30),
                              ("[2] 참고 — 60영업일", met60, d60)):
        add("=" * 92)
        add(title)
        add("=" * 92)
        add(f"{'지표':<24}" + "".join(f"{v:>20}" for v in VARIANTS))
        add("-" * 92)
        for name, k, fmt in rows:
            vals = []
            for v in VARIANTS:
                m = met[v]
                if not m:
                    vals.append("-")
                elif fmt == "days":
                    vals.append(f"{m['profit_days']}/{m['loss_days']}")
                elif fmt == "streak":
                    vals.append(f"{m['max_consec_losses']}건 {m['max_consec_losses_pct']:+.2f}%")
                elif k is None:
                    vals.append(f"{m['wins']}/{m['losses']}")
                else:
                    x = m.get(k)
                    vals.append("n/a" if x is None else fmt.format(x))
            add(f"{name:<24}" + "".join(f"{s:>20}" for s in vals))
        add("")
        for v in VARIANTS:
            add(f"  {v} = {LABEL[v]}")
        add("")

    # ── SNAPBACK 거래 상세 ──
    add("=" * 92)
    add("[3] SNAPBACK 거래 전부 (B)")
    add("=" * 92)
    snaps_all = [t for t in tv["B"] if t["is_snapback"]]
    snaps30 = [t for t in snaps_all if t["date"] in set(d30)]
    for scope, lst in (("30영업일", snaps30), ("60영업일 전체", snaps_all)):
        add(f"-- {scope}: {len(lst)}건")
        if lst:
            add(f"   {'날짜':<10}{'진입':<7}{'청산':<7}{'방향':<11}{'종목':<9}"
                f"{'진입가':>9}{'청산가':>9}{'손익%':>9}{'MFE%':>8}{'조건':>6}  청산사유")
            add("   " + "-" * 105)
        for t in lst:
            add(f"   {t['date']:<10}{hhmm(t['entry_time']):<7}{hhmm(t['exit_time']):<7}"
                f"{t['direction']:<11}"
                f"{SYMBOL_NAME.get(t['entry_symbol'], t['entry_symbol']):<9}"
                f"{t['entry_price']:>9.0f}{t['exit_price']:>9.0f}"
                f"{t['net_pct']:>+9.3f}{t['peak_net_pct']:>+8.2f}"
                f"{str(t['snap_passed']) + '/4':>6}  {t['exit_reason']}")
        add("")
        if lst:
            for t in lst:
                c = t["snap_conditions"] or {}
                sw = t.get("switched_from")
                add(f"     {t['date']} {hhmm(t['entry_time'])}: "
                    + " ".join(f"{k.split('_',1)[0]}={'O' if v else 'X'}" for k, v in c.items())
                    + f"  보유전환={'있음(' + SYMBOL_NAME.get(sw, sw) + ')' if sw else '없음'}"
                    + f"  CHOP={t['entry_chop']} 조기익절발동={t['lock_fired']} "
                      f"보유 {t['hold_bars']}봉")
            add("")

    add("=" * 92)
    add("[4] SNAPBACK 거래만 따로 본 성과")
    add("=" * 92)
    for scope, lst in (("30영업일", snaps30), ("60영업일", snaps_all)):
        s = sub(lst)
        add(f"-- {scope}")
        for k, v in s.items():
            add(f"     {k:<22} {v}")
        add("")
    base30 = [t for t in tv["B"] if not t["is_snapback"] and t["date"] in set(d30)]
    add("   (참고) B 의 기존 3-slot 거래만(30일): " + json.dumps(sub(base30), ensure_ascii=False))
    add("   (참고) A 전체(30일):                  "
        + json.dumps(sub([t for t in tv["A"] if t["date"] in set(d30)]), ensure_ascii=False))
    add("")

    # ── SNAPBACK 후보 전체 (승인/거절) ──
    add("=" * 92)
    add("[5] SNAPBACK 후보 도달 전체 내역 (13:00~14:50 & 3슬롯 소진 후)")
    add("=" * 92)
    cands = raw["snap_candidates_B"]
    c30 = [c for c in cands if c["date"] in set(d30)]
    add(f"60일 {len(cands)}건 / 30일 {len(c30)}건, 승인 "
        f"{sum(1 for c in cands if c['approved'])}건 (30일 "
        f"{sum(1 for c in c30 if c['approved'])}건)")
    add(f"{'날짜':<10}{'판정':<7}{'방향':<11}{'슬롯':>5}{'통과':>6}"
        f"{'극값이격':>10}{'VWAP':>9}{'slope':>9}{'gap':>11}  결과")
    add("-" * 92)
    for c in cands:
        mark = "" if c["date"] in set(d30) else " (60일전용)"
        res = "진입" if c["approved"] else (c["reject"] or "")
        def fmt(x, w, p):
            return f"{x:>{w}.{p}f}" if x is not None else f"{'-':>{w}}"
        add(f"{c['date']:<10}{hhmm(c['decision_at']):<7}{c['direction']:<11}"
            f"{c['slots_used_before']:>5}{str(c['passed']) + '/4':>6}"
            f"{fmt(c['day_extreme_margin_pct'], 10, 2)}{fmt(c['price_vs_vwap_pct'], 9, 2)}"
            f"{fmt(c['ema20_slope_pct'], 9, 3)}{fmt(c['gap_signed'], 11, 1)}  {res}{mark}")
    add("")
    add("   조건별 충족 빈도(후보 전체):")
    if cands:
        for key in ("C1_day_extreme", "C2_vwap", "C3_ema20_slope", "C4_t3_hold"):
            n = sum(1 for c in cands if c["conditions"].get(key))
            add(f"     {key:<18} {n}/{len(cands)}건")
    add("")
    add("   참고: 이 후보들이 현행 A 에서 막힌 TW2 사유")
    from collections import Counter
    cnt = Counter(c["tw2_block_reason"] for c in cands)
    for k, v in cnt.most_common():
        add(f"     {k:<40} {v}건")
    add("")

    # ── 날짜별 A/B ──
    add("=" * 92)
    add("[6] SNAPBACK 이 발생한 날의 A/B 비교 (30일 구간)")
    add("=" * 92)
    snap_days = sorted({t["date"] for t in snaps30})
    for d in snap_days:
        add(f"### {d}")
        for v in ("A", "B"):
            ts = [t for t in tv[v] if t["date"] == d]
            add(f"  {v}: {len(ts)}건 합계 {sum(t['net_pct'] for t in ts):+.3f}%")
            for t in ts:
                mark = "★SNAP" if t.get("is_snapback") else f"S{t['slot_number']}"
                add(f"      {hhmm(t['entry_time'])}->{hhmm(t['exit_time'])} {mark} "
                    f"{t['direction'][:4]} {t['net_pct']:+.2f}% ({t['exit_reason']})")
        da = sum(t["net_pct"] for t in tv["A"] if t["date"] == d)
        db = sum(t["net_pct"] for t in tv["B"] if t["date"] == d)
        add(f"  => B-A = {db - da:+.3f}%")
        add("")

    # 전체 날짜 차이
    add("-- 30일 전 구간 날짜별 차이 (B-A, 0 아닌 날만)")
    diffs = []
    for d in d30:
        da = sum(t["net_pct"] for t in tv["A"] if t["date"] == d)
        db = sum(t["net_pct"] for t in tv["B"] if t["date"] == d)
        if abs(db - da) > 1e-9:
            diffs.append((d, da, db, db - da))
    for d, da, db, x in diffs:
        add(f"   {d}  A {da:+8.3f}%  B {db:+8.3f}%  차이 {x:+8.3f}%")
    add(f"   좋아진 날 {sum(1 for x in diffs if x[3] > 0)}일 / "
        f"나빠진 날 {sum(1 for x in diffs if x[3] < 0)}일 / "
        f"동일 {len(d30) - len(diffs)}일   합계 {sum(x[3] for x in diffs):+.3f}%")
    add("")

    (BASE / "report.txt").write_text("\n".join(L), encoding="utf-8")
    (BASE / "report.json").write_text(json.dumps({
        "metrics_30d": met30, "metrics_60d": met60,
        "snapback_trades_30d": snaps30, "snapback_trades_60d": snaps_all,
        "snapback_sub_30d": sub(snaps30), "snapback_sub_60d": sub(snaps_all),
        "candidates": cands,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {BASE / 'report.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
