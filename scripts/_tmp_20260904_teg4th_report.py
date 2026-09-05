"""raw.json(_tmp_20260904_teg4th_fullchain.py 산출물) 기반 리포트 집계.
READ-ONLY, production 무관. report.txt(UTF-8) + report.json 저장."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
BASE = PROJECT_ROOT / "data" / "validation" / "teg4th_fullchain"
RAW = BASE / "raw.json"
OUT_JSON = BASE / "report.json"
OUT_TXT = BASE / "report.txt"

VARIANTS = ["A", "B", "B_STRICT"]
LABEL = {
    "A": "A 현행 (TW2 3-SLOT + 조기익절)",
    "B": "B A + 오후 TEGv2 추가 1회",
    "B_STRICT": "B_STRICT (B + 오후 동일방향 금지규칙까지 적용)",
}
SYMBOL_NAME = {"0193T0": "LONG", "0197X0": "INVERSE"}


def metrics(trades: list[dict], n_days: int) -> dict:
    if not trades:
        return {}
    df = pd.DataFrame(trades).sort_values("exit_time").reset_index(drop=True)
    n = len(df)
    wins = df[df["net_pct"] > 0]
    losses = df[df["net_pct"] < 0]
    equity = (1 + df["net_pct"] / 100).cumprod()
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
        "simple_cum_return_pct": round(float(df["net_pct"].sum()), 4),
        "compound_cum_return_pct": round(float((equity.iloc[-1] - 1) * 100), 4),
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
        "trades_ge_3pct": int((df["net_pct"] >= 3.0).sum()),
        "trades_ge_5pct": int((df["net_pct"] >= 5.0).sum()),
    }


def sub_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {}
    df = pd.DataFrame(trades)
    wins = df[df["net_pct"] > 0]
    losses = df[df["net_pct"] < 0]
    gp = wins["net_pct"].sum()
    gl = -losses["net_pct"].sum()
    return {
        "trades": len(df),
        "wins": int(len(wins)), "losses": int(len(losses)),
        "win_rate_pct": round(len(wins) / len(df) * 100, 2),
        "avg_return_per_trade_pct": round(float(df["net_pct"].mean()), 4),
        "profit_factor": round(float(gp / gl), 4) if gl > 0 else None,
        "total_simple_pct": round(float(df["net_pct"].sum()), 4),
        "gross_profit_pct": round(float(gp), 4),
        "gross_loss_pct": round(float(-gl), 4),
        "best_pct": round(float(df["net_pct"].max()), 4),
        "worst_pct": round(float(df["net_pct"].min()), 4),
        "trades_ge_3pct": int((df["net_pct"] >= 3.0).sum()),
    }


def key_of(t: dict) -> tuple:
    return (t["decision_idx"], t["direction"])


def hhmm(iso):
    return iso[11:16] if iso else "-"


def brief(t: dict) -> str:
    mark = "★추가" if t.get("is_extra") else f"S{t['slot_number']}"
    return (f"{hhmm(t['entry_time'])}->{hhmm(t['exit_time'])} {mark} #{t['flag_ordinal']} "
            f"{t['direction'][:4]} {t['net_pct']:+.2f}% ({t['exit_reason']})")


def main() -> int:
    raw = json.loads(RAW.read_text(encoding="utf-8"))
    dates = raw["dates"]
    n_days = len(dates)
    tv = {v: raw[v] for v in VARIANTS}

    L: list[str] = []
    add = L.append
    add(f"기간: {dates[0]} ~ {dates[-1]}  ({n_days}영업일)")
    add("")

    met = {v: metrics(tv[v], n_days) for v in VARIANTS}

    # ── 0) 종합 지표 ───────────────────────────────────────────────
    add("=" * 96)
    add("[0] 종합 지표")
    add("=" * 96)
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
        ("+3% 이상 거래(건)", "trades_ge_3pct", "{:.0f}"),
        ("+5% 이상 거래(건)", "trades_ge_5pct", "{:.0f}"),
    ]
    add(f"{'지표':<24}" + "".join(f"{v:>16}" for v in VARIANTS))
    add("-" * 96)
    for name, k, fmt in rows:
        if k is None:
            vals = [f"{met[v]['wins']}/{met[v]['losses']}/{met[v]['breakeven']}" for v in VARIANTS]
        else:
            vals = ["n/a" if met[v].get(k) is None else fmt.format(met[v][k]) for v in VARIANTS]
        add(f"{name:<24}" + "".join(f"{s:>16}" for s in vals))
    add("")
    for v in VARIANTS:
        add(f"  {v} = {LABEL[v]}")
    add("")
    add(f"B == B_STRICT: {raw['B_equals_STRICT']}")
    add("")

    # ── 1) 추가 TEGv2 거래 전부 ────────────────────────────────────
    add("=" * 96)
    add("[1] B 에서 발생한 오후 추가 TEGv2 거래 전부")
    add("=" * 96)
    extras = [t for t in tv["B"] if t.get("is_extra")]
    add(f"{'날짜':<10}{'진입':<7}{'청산':<7}{'방향':<11}{'종목':<9}"
        f"{'진입가':>9}{'청산가':>9}{'손익%':>9}{'MFE%':>8}  청산사유")
    add("-" * 110)
    for t in extras:
        add(f"{t['date']:<10}{hhmm(t['entry_time']):<7}{hhmm(t['exit_time']):<7}"
            f"{t['direction']:<11}{SYMBOL_NAME.get(t['entry_symbol'], t['entry_symbol']):<9}"
            f"{t['entry_price']:>9.0f}{t['exit_price']:>9.0f}{t['net_pct']:>+9.3f}"
            f"{t['peak_net_pct']:>+8.2f}  {t['exit_reason']}")
    add("")
    for t in extras:
        sw = t.get("switched_from")
        add(f"  {t['date']} {hhmm(t['entry_time'])}: 그날 {t['flag_ordinal']}번째 플래그, "
            f"보유전환={'있음(' + SYMBOL_NAME.get(sw, sw) + ' 청산 후 스위치)' if sw else '없음(플랫에서 신규)'}, "
            f"보유 {t['hold_bars']}봉, CHOP진입={t['entry_chop']}, 조기익절발동={t['lock_fired']}")
    add("")

    # 후보 도달/거절 내역
    add("-- 오후 4번째 후보 도달 전체 내역 (TEGv2 판정) --")
    for e in raw["extra_candidates_B"]:
        st = "진입" if e["entered"] else f"거절({e['reject_reason']})"
        add(f"   {e['date']} {hhmm(e['decision_at'])} {e['direction']:<10} "
            f"그날 {e['flag_ordinal']}번째  TEGv2={e['teg_approved']}  -> {st}")
        if not e["teg_approved"]:
            add(f"        TEG 미충족 조건: {', '.join(e['teg_reject_reasons'])}")
    add("")

    # ── 2) 추가거래만 따로 ─────────────────────────────────────────
    add("=" * 96)
    add("[2] 추가 TEGv2 거래만 따로 본 성과")
    add("=" * 96)
    sm = sub_metrics(extras)
    for k, v in sm.items():
        add(f"   {k:<28} {v}")
    add("")
    base_b = [t for t in tv["B"] if not t.get("is_extra")]
    add("   (참고) B 의 기존 3-slot 거래만: " + json.dumps(sub_metrics(base_b), ensure_ascii=False))
    add("   (참고) A 전체:                  " + json.dumps(sub_metrics(tv["A"]), ensure_ascii=False))
    add("")

    # ── 3) +3% 이상 거래 ───────────────────────────────────────────
    add("=" * 96)
    add("[3] +3% 이상 큰 수익 거래 건수")
    add("=" * 96)
    add(f"   추가 TEGv2 거래 중 +3% 이상: {sm.get('trades_ge_3pct', 0)}건 / 전체 {len(extras)}건")
    for t in extras:
        if t["net_pct"] >= 3.0:
            add(f"      {t['date']} {hhmm(t['entry_time'])} {t['direction']} {t['net_pct']:+.3f}%")
    add("")
    for v in VARIANTS:
        add(f"   {v} 전체 +3% 이상: {met[v]['trades_ge_3pct']}건 / +5% 이상: {met[v]['trades_ge_5pct']}건")
    add("")

    # ── 4) 추가거래가 기존 거래에 미친 영향 ────────────────────────
    add("=" * 96)
    add("[4] 추가거래 때문에 기존 3-slot 거래의 청산/후속이 달라진 사례")
    add("=" * 96)
    a_by = {key_of(t): t for t in tv["A"]}
    b_by = {key_of(t): t for t in tv["B"]}
    changed = []
    for k, at in a_by.items():
        btr = b_by.get(k)
        if btr is None:
            changed.append(("A에만 있음(B에서 사라짐)", at, None))
        elif (at["exit_time"] != btr["exit_time"] or at["exit_reason"] != btr["exit_reason"]
              or round(at["net_pct"], 6) != round(btr["net_pct"], 6)):
            changed.append(("청산이 달라짐", at, btr))
    for k, btr in b_by.items():
        if k not in a_by and not btr.get("is_extra"):
            changed.append(("B에만 있는 신규 3-slot 거래", None, btr))
    if not changed:
        add("   기존 3-slot 거래의 진입/청산/손익이 A와 100% 동일 — 추가거래는 순수 가산이었음.")
        add("   (추가 7건 모두 그날 3슬롯이 이미 소진되고 기존 포지션이 정리된 뒤,")
        add("    또는 스위치로 기존 포지션을 production 방식대로 청산한 뒤 발생)")
    for kind, at, btr in changed:
        add(f"   [{kind}]")
        if at:
            add(f"      A: {at['date']} {brief(at)}")
        if btr:
            add(f"      B: {btr['date']} {brief(btr)}")
    add("")
    # 스위치로 기존 포지션을 청산시킨 추가거래
    sw = [t for t in extras if t.get("switched_from")]
    add(f"-- 추가거래가 반대신호 스위치로 기존 포지션을 청산시킨 사례: {len(sw)}건")
    for t in sw:
        prior = None
        for x in tv["B"]:
            if (x["date"] == t["date"] and x["exit_time"] == t["entry_time"]
                    and x["entry_symbol"] == t["switched_from"]):
                prior = x
                break
        add(f"   {t['date']} {hhmm(t['entry_time'])}: "
            f"{SYMBOL_NAME.get(t['switched_from'], t['switched_from'])} 청산"
            + (f" ({prior['net_pct']:+.3f}%, {prior['exit_reason']})" if prior else "")
            + f" -> {SYMBOL_NAME.get(t['entry_symbol'], t['entry_symbol'])} 추가진입 "
              f"({t['net_pct']:+.3f}%)")
        if prior:
            a_prior = a_by.get(key_of(prior))
            if a_prior:
                add(f"      A에서 같은 거래는: {hhmm(a_prior['exit_time'])} "
                    f"{a_prior['net_pct']:+.3f}% ({a_prior['exit_reason']})  "
                    f"-> 차이 {prior['net_pct'] - a_prior['net_pct']:+.3f}%p")
    add("")

    # ── 5) 최근 5영업일 A/B 비교 ───────────────────────────────────
    add("=" * 96)
    add("[5] 최근 5영업일 A/B 날짜별 비교")
    add("=" * 96)
    timeline = {}
    for d in dates[-5:]:
        add(f"### {d}")
        timeline[d] = {}
        for v in ("A", "B"):
            ts = [t for t in tv[v] if t["date"] == d]
            timeline[d][v] = ts
            add(f"  {v}: {len(ts)}건 합계 {sum(t['net_pct'] for t in ts):+.3f}%")
            for t in ts:
                add(f"      {brief(t)}")
        ec = [e for e in raw["extra_candidates_B"] if e["date"] == d]
        for e in ec:
            add(f"      [4번째 후보] {hhmm(e['decision_at'])} {e['direction']} "
                f"TEGv2={e['teg_approved']} -> "
                f"{'진입' if e['entered'] else e['reject_reason']}")
        da = sum(t["net_pct"] for t in tv["A"] if t["date"] == d)
        db = sum(t["net_pct"] for t in tv["B"] if t["date"] == d)
        add(f"  => B-A = {db - da:+.3f}%")
        add("")

    # ── 6) 전체 날짜별 차이 ────────────────────────────────────────
    add("=" * 96)
    add("[6] 전 기간 날짜별 A/B 손익")
    add("=" * 96)
    daily = {}
    for v in VARIANTS:
        s = pd.DataFrame(tv[v]).groupby("date")["net_pct"].sum()
        daily[v] = {d: float(s.get(d, 0.0)) for d in dates}
    add(f"{'date':<10}{'A':>11}{'B':>11}{'B-A':>10}   추가거래")
    add("-" * 60)
    for d in dates:
        ex = [t for t in tv["B"] if t["date"] == d and t.get("is_extra")]
        tag = "  ".join(f"{hhmm(t['entry_time'])} {t['net_pct']:+.2f}%" for t in ex)
        add(f"{d:<10}{daily['A'][d]:>+11.3f}{daily['B'][d]:>+11.3f}"
            f"{daily['B'][d] - daily['A'][d]:>+10.3f}   {tag}")
    add("")
    better = [(d, daily["B"][d] - daily["A"][d]) for d in dates if daily["B"][d] - daily["A"][d] > 1e-9]
    worse = [(d, daily["B"][d] - daily["A"][d]) for d in dates if daily["B"][d] - daily["A"][d] < -1e-9]
    add(f"좋아진 날 {len(better)}일 (합 {sum(x for _, x in better):+.3f}%) / "
        f"나빠진 날 {len(worse)}일 (합 {sum(x for _, x in worse):+.3f}%) / "
        f"동일 {n_days - len(better) - len(worse)}일")
    add("")

    OUT_TXT.write_text("\n".join(L), encoding="utf-8")
    OUT_JSON.write_text(json.dumps({
        "period": {"start": dates[0], "end": dates[-1], "n_days": n_days},
        "metrics": met,
        "extra_trades": extras,
        "extra_metrics": sm,
        "extra_candidates": raw["extra_candidates_B"],
        "changed_existing": [
            {"kind": k, "A": a, "B": b} for k, a, b in changed],
        "daily": daily,
        "timeline_last5": timeline,
        "B_equals_STRICT": raw["B_equals_STRICT"],
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"saved {OUT_TXT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
