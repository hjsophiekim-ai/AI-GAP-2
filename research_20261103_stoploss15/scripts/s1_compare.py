"""손절선 변경 A/B 집계 — A=L85_A(현행 -1.3%) vs B=SL15(-1.5%).

B 는 'TW 손절이 있던 날'만 재생했다. 나머지 날은 손절선이 넓어져도 발동 자체가
없으므로 A 와 동일한 것이 코드상 보장된다 -> A 산출로 채운다.
"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
A_OUT = Path(r"C:/Users/Kim Hyunjoo/AppData/Local/Temp/claude/arm3_sp/out")
B_OUT = Path(os.environ["B_OUT"])
FEE = 0.000036
FROM = "20260803"


def episodes(js, day):
    eps, cur = [], None
    for o in js["orders"]:
        if o["t"][:10].replace("-", "") != day:
            continue
        if o["side"] == "BUY":
            if cur is not None and cur["left"] > 0:
                cur["buys"].append(o); cur["left"] += o["qty"]; continue
            cur = dict(sym=o["sym"], buys=[o], sells=[], left=o["qty"])
            eps.append(cur)
        elif cur is not None:
            cur["sells"].append(o); cur["left"] -= o["qty"]
    out = []
    for ep in eps:
        b = ep["buys"][0]; e = float(b["px"])
        pnl = sum(s["qty"] * (s["px"] - e) - FEE * s["qty"] * (s["px"] + e) for s in ep["sells"])
        out.append(dict(day=day, t=b["t"][11:19], sym=ep["sym"], px=e, qty=b["qty"], pnl=pnl,
                        exit=ep["sells"][-1]["t"][11:19] if ep["sells"] else "",
                        why=";".join(sorted({a.split(":")[0] for s in ep["sells"]
                                             for a in (s.get("acts") or [])}))))
    return out


def load(tag, out_dir, days):
    rows = {}
    for d in days:
        f = Path(out_dir) / f"{tag}_{d}.json"
        if not f.exists():
            f = A_OUT / f"L85_A_{d}.json"          # 재생 생략일 = A 와 동일
        rows[d] = episodes(json.loads(f.read_text(encoding="utf-8")), d)
    return rows


def metrics(rows, days):
    tr = [t for d in days for t in rows[d]]
    daily = [sum(t["pnl"] for t in rows[d]) for d in days]
    cum, peak, mdd = 0.0, 0.0, 0.0
    for x in daily:
        cum += x; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    g = sum(t["pnl"] for t in tr if t["pnl"] > 0)
    l = -sum(t["pnl"] for t in tr if t["pnl"] < 0)
    top = sorted((t["pnl"] for t in tr), reverse=True)
    return dict(n=len(tr), total=sum(t["pnl"] for t in tr), pf=(g / l if l else float("inf")),
                mdd=mdd, worst=min(daily), worst_day=days[daily.index(min(daily))],
                win=100.0 * sum(1 for t in tr if t["pnl"] > 0) / max(len(tr), 1),
                ex1=sum(t["pnl"] for t in tr) - sum(top[:1]),
                ex2=sum(t["pnl"] for t in tr) - sum(top[:2]),
                loss_days=sum(1 for x in daily if x < 0))


days = sorted(f.stem.split("_")[-1] for f in A_OUT.glob("L85_A_2026*.json")
              if f.stem.split("_")[-1] >= FROM)
A, B = load("L85_A", A_OUT, days), load("SL15", B_OUT, days)
sa, sb = metrics(A, days), metrics(B, days)
print(f"A=현행 -1.3%   B=-1.5%   {len(days)}일 {days[0]}~{days[-1]}\n")
print(f"{'':12s}{'A':>14s}{'B':>14s}{'Δ':>14s}")
for k, lab, f in (("n", "거래수", "{:14,.0f}"), ("total", "총손익", "{:+14,.0f}"),
                  ("pf", "PF", "{:14.3f}"), ("mdd", "MDD", "{:14,.0f}"),
                  ("worst", "최악일", "{:+14,.0f}"), ("win", "승률%", "{:14.1f}"),
                  ("loss_days", "손실일수", "{:14,.0f}"),
                  ("ex1", "상위1제외", "{:+14,.0f}"), ("ex2", "상위2제외", "{:+14,.0f}")):
    d = sb[k] - sa[k]
    print(f"{lab:12s}{f.format(sa[k])}{f.format(sb[k])}{('{:+14.3f}' if k=='pf' else '{:+14,.2f}').format(d)}")
print(f"  최악일자 A {sa['worst_day']}  B {sb['worst_day']}")

print("\n[달라진 거래 전수]")
tot = 0.0
for d in days:
    ka = {(t["t"], t["sym"]): t for t in A[d]}
    kb = {(t["t"], t["sym"]): t for t in B[d]}
    for k in sorted(set(ka) | set(kb)):
        ra, rb = ka.get(k), kb.get(k)
        if ra and rb and abs(ra["pnl"] - rb["pnl"]) < 1 and ra["exit"] == rb["exit"]:
            continue
        fa = f'{ra["t"]} @{ra["px"]:,.0f} -> {ra["exit"]} {ra["why"][:26]:26s} {ra["pnl"]:+11,.0f}' if ra else "-" * 26
        fb = f'{rb["t"]} @{rb["px"]:,.0f} -> {rb["exit"]} {rb["why"][:26]:26s} {rb["pnl"]:+11,.0f}' if rb else "-" * 26
        dl = (rb["pnl"] if rb else 0) - (ra["pnl"] if ra else 0)
        tot += dl
        print(f"  {d} {k[1]} | A {fa} | B {fb} | Δ {dl:+11,.0f}")
print(f"  합계 Δ {tot:+,.0f}")

print("\n[현행 손절(TIME_WINDOW_STOP_LOSS) 거래가 -1.5% 에서 어떻게 됐나]")
n_saved = n_worse = 0
sl_delta = 0.0
for d in days:
    kb = {(t["t"], t["sym"]): t for t in B[d]}
    for t in A[d]:
        if "TIME_WINDOW_STOP_LOSS" not in t["why"]:
            continue
        m = kb.get((t["t"], t["sym"]))
        dl = (m["pnl"] if m else 0) - t["pnl"]
        sl_delta += dl
        n_saved += dl > 0
        n_worse += dl < 0
        print(f"  {d} {t['t']} {t['sym']} A {t['pnl']:+10,.0f} -> B "
              f"{(m['pnl'] if m else 0):+10,.0f} ({m['why'][:22] if m else '진입없음'})  Δ {dl:+10,.0f}")
print(f"  손절 {n_saved + n_worse + 0}건 중 개선 {n_saved} / 악화 {n_worse}   그 거래들 Δ 합 {sl_delta:+,.0f}")
