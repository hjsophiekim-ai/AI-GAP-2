"""재생 산출(json) → 거래 에피소드 + 변형별 지표/Δ.

    python .../a2_metrics.py A=TAG_A B=TAG_B C=TAG_C --days 20261016 ... --label CHOP

함정 (앞선 연구에서 확인된 것):
  · `ex` 원장의 timestamp 는 **벽시계**다. 재생 시각은 `orders[].t` 에만 있다.
  · `ex` 는 전일(P) 선재생분까지 들고 있다 — BUY 의 signal_id 날짜로 그 날만 걸러야 한다.
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
OUT = Path(os.environ.get("ARM3_OUT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/out"))


def episodes(path, day):
    """그 날 체결된 포지션 단위 에피소드. 반환 = [dict(entry_t, exit_t, sym, qty, entry_px, pnl, ret_pct, exits)]"""
    d = json.loads(Path(path).read_text(encoding="utf-8"))
    buys = [o for o in d["orders"] if o["side"] == "BUY"]
    sells = [o for o in d["orders"] if o["side"] == "SELL"]
    eps, cur, bi, si = [], None, 0, 0
    for r in d["ex"]:
        qty = int(float(r["executed_qty"] or 0))
        px = float(r["executed_price"] or 0)
        if r["side"] == "BUY":
            sid = r.get("signal_id") or ""
            if cur is not None:                      # 이전 에피소드가 안 닫혔다 — 보호적으로 닫는다
                eps.append(cur)
            today = sid[:8] == day
            t = buys[bi]["t"] if (today and bi < len(buys)) else None
            if today:
                bi += 1
            cur = dict(day=day, sym=r["symbol"], entry_t=t, entry_px=px, qty=qty, open_qty=qty,
                       today=today, sid=sid, pnl=0.0, exits=[], exit_t=None, exit_px=0.0, sold=0)
            continue
        if cur is None:
            continue
        cur["pnl"] += float(r["net_pnl"] or 0)
        cur["exits"].append(r.get("exit_reason") or "")
        cur["exit_px"] += px * qty
        cur["sold"] += qty
        cur["open_qty"] -= qty
        if cur["today"] and si < len(sells):
            cur["exit_t"] = sells[si]["t"]
            si += 1
        if cur["open_qty"] <= 0:
            eps.append(cur)
            cur = None
    if cur is not None:
        eps.append(cur)
    out = []
    for e in eps:
        if not e["today"]:
            continue
        e["exit_px"] = (e["exit_px"] / e["sold"]) if e["sold"] else 0.0
        e["ret_pct"] = (e["exit_px"] / e["entry_px"] - 1) * 100 if e["entry_px"] else 0.0
        e["net_pct"] = e["pnl"] / (e["entry_px"] * e["qty"]) * 100 if e["entry_px"] and e["qty"] else 0.0
        out.append(e)
    return out


def load(tag, days):
    eps, miss = [], []
    for d in days:
        p = OUT / f"{tag}_{d}.json"
        if not p.exists():
            miss.append(d)
            continue
        eps.extend(episodes(p, d))
    return eps, miss


def mdd(daily):
    """일별 손익 리스트 → 누적곡선 최대낙폭(원, 양수)."""
    peak, cum, dd = 0.0, 0.0, 0.0
    for v in daily:
        cum += v
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def stats(eps, days):
    by_day = {d: 0.0 for d in days}
    for e in eps:
        by_day[e["day"]] = by_day.get(e["day"], 0.0) + e["pnl"]
    pnls = [e["pnl"] for e in eps]
    win = [p for p in pnls if p > 0]
    loss = [p for p in pnls if p <= 0]
    daily = [by_day[d] for d in days]
    top = sorted(pnls, reverse=True)
    return dict(
        n=len(eps), total=sum(pnls),
        pf=(sum(win) / abs(sum(loss))) if loss and sum(loss) else float("inf"),
        mdd=mdd(daily), worst_day=min(daily) if daily else 0.0,
        worst_day_at=(days[daily.index(min(daily))] if daily else None),
        win_rate=(len(win) / len(eps) * 100) if eps else 0.0,
        ex_top1=sum(pnls) - (top[0] if top else 0),
        ex_top2=sum(pnls) - sum(top[:2]),
        by_day=by_day,
    )


def key(e):
    """변형 간 같은 거래를 잇는 키 — 진입 시각이 같으면 같은 거래로 본다."""
    return (e["day"], e["entry_t"] or "", e["sym"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pairs", nargs="+", help="NAME=TAG")
    ap.add_argument("--days", nargs="+", required=True)
    ap.add_argument("--label", default="")
    ap.add_argument("--csv", default=None)
    a = ap.parse_args()

    variants = {}
    for p in a.pairs:
        name, tag = p.split("=", 1)
        eps, miss = load(tag, a.days)
        if miss:
            print(f"  !! {name}({tag}) 결측일 {miss}")
        variants[name] = eps
    names = list(variants)
    base = names[0]

    print(f"\n{'='*96}\n{a.label}  변형 {names}  대상 {len(a.days)}일 {a.days[0]}~{a.days[-1]}\n{'='*96}")
    S = {n: stats(variants[n], a.days) for n in names}
    hdr = f"{'':10s}" + "".join(f"{n:>17s}" for n in names)
    print(hdr)
    rows = [("거래수", "n", "{:,.0f}"), ("총손익", "total", "{:+,.0f}"), ("PF", "pf", "{:.3f}"),
            ("MDD", "mdd", "{:,.0f}"), ("최악일", "worst_day", "{:+,.0f}"), ("승률%", "win_rate", "{:.1f}"),
            ("상위1제외", "ex_top1", "{:+,.0f}"), ("상위2제외", "ex_top2", "{:+,.0f}")]
    for label, k, fmt in rows:
        print(f"{label:10s}" + "".join(f"{fmt.format(S[n][k]):>17s}" for n in names))
    print(f"{'최악일자':10s}" + "".join(f"{str(S[n]['worst_day_at']):>17s}" for n in names))
    print(f"\n[Δ vs {base}]")
    for n in names[1:]:
        print(f"  {n:12s} 총손익 {S[n]['total']-S[base]['total']:+,.0f}  PF {S[n]['pf']-S[base]['pf']:+.3f}  "
              f"MDD {S[n]['mdd']-S[base]['mdd']:+,.0f}  최악일 {S[n]['worst_day']-S[base]['worst_day']:+,.0f}  "
              f"승률 {S[n]['win_rate']-S[base]['win_rate']:+.1f}%p  "
              f"상위1제외 {S[n]['ex_top1']-S[base]['ex_top1']:+,.0f}  상위2제외 {S[n]['ex_top2']-S[base]['ex_top2']:+,.0f}")

    print("\n[일별 손익]")
    print(f"{'날짜':10s}" + "".join(f"{n:>15s}" for n in names))
    for d in a.days:
        print(f"{d:10s}" + "".join(f"{S[n]['by_day'].get(d,0.0):>+15,.0f}" for n in names))

    # ── 같은 거래 1:1 대조 ───────────────────────────────────────────────
    idx = {n: {key(e): e for e in variants[n]} for n in names}
    allk = sorted({k for n in names for k in idx[n]})
    print(f"\n[거래 1:1 대조]  (진입시각 기준. {base} 에 없는 거래는 '-')")
    print(f"{'진입':20s}{'종목':8s}" + "".join(f"{n+'손익':>14s}{n+'수익%':>9s}" for n in names) + "  사유")
    for k in allk:
        line = f"{(k[1][11:19] if k[1] else '?')+' '+k[0]:20s}{k[2]:8s}"
        for n in names:
            e = idx[n].get(k)
            line += (f"{e['pnl']:>+14,.0f}{e['ret_pct']:>9.2f}" if e else f"{'-':>14s}{'-':>9s}")
        e0 = idx[base].get(k)
        ex = [idx[n].get(k) for n in names]
        line += "  " + " | ".join(",".join(sorted(set(x["exits"]))) if x else "-" for x in ex)
        print(line)

    # ── 큰 승자 훼손 ─────────────────────────────────────────────────────
    print(f"\n[{base} 기준 큰 승자의 훼손액]")
    for thr in (4.0, 5.0, 7.0):
        bigs = [e for e in variants[base] if e["ret_pct"] >= thr]
        if not bigs:
            print(f"  +{thr:.0f}% 이상: 0건")
            continue
        line = f"  +{thr:.0f}% 이상 {len(bigs):2d}건  {base} {sum(e['pnl'] for e in bigs):+,.0f}"
        for n in names[1:]:
            v = sum((idx[n].get(key(e), {}) or {}).get("pnl", 0.0) for e in bigs)
            line += f"   {n} {v:+,.0f} (Δ{v - sum(e['pnl'] for e in bigs):+,.0f})"
        print(line)

    if a.csv:
        import csv
        with open(a.csv, "w", newline="", encoding="utf-8-sig") as fh:
            w = csv.writer(fh)
            w.writerow(["variant", "day", "entry_t", "exit_t", "sym", "qty", "entry_px", "exit_px",
                        "pnl", "ret_pct", "exits"])
            for n in names:
                for e in variants[n]:
                    w.writerow([n, e["day"], e["entry_t"], e["exit_t"], e["sym"], e["qty"],
                                e["entry_px"], round(e["exit_px"], 2), round(e["pnl"], 0),
                                round(e["ret_pct"], 3), ";".join(e["exits"])])
        print(f"\n-> {a.csv}")


if __name__ == "__main__":
    main()
