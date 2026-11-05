"""규칙 B 재생 A/B 집계 — A=L85_A(현행) vs B=CHK_SLH_ON(초기 30분 손절 2봉 확인).

B 는 '현행에서 TIME_WINDOW_STOP_LOSS 가 있던 18일'만 재생했다. 나머지 40일은
유예 대상 자체가 없어 코드상 A 와 동일하므로 A 산출로 채운다.
재현: python research_20261105_early_stop_2bar/scripts/s2_compare.py
"""
import json
import os
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
OUTD = Path(os.environ.get("ARM3_OUT",
            r"C:/Users/Kim Hyunjoo/AppData/Local/Temp/claude/arm3_sp/out"))
FEE = 0.000036
FROM = "20260803"
TAG_B = "CHK_SLH_ON"


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
                        exit_px=float(ep["sells"][-1]["px"]) if ep["sells"] else 0.0,
                        why=";".join(sorted({a.split(":")[0] for s in ep["sells"]
                                             for a in (s.get("acts") or [])}))))
    return out


def load(tag, days):
    rows, logs = {}, {}
    for d in days:
        f = OUTD / f"{tag}_{d}.json"
        if not f.exists():
            f = OUTD / f"L85_A_{d}.json"          # 재생 생략일 = A 와 동일
        js = json.loads(f.read_text(encoding="utf-8"))
        rows[d] = episodes(js, d)
        logs[d] = js.get("slh") or []
    return rows, logs


def metrics(rows, days):
    tr = [t for d in days for t in rows[d]]
    daily = [sum(t["pnl"] for t in rows[d]) for d in days]
    cum = peak = mdd = 0.0
    for x in daily:
        cum += x; peak = max(peak, cum); mdd = max(mdd, peak - cum)
    g = sum(t["pnl"] for t in tr if t["pnl"] > 0)
    l = -sum(t["pnl"] for t in tr if t["pnl"] < 0)
    top = sorted((t["pnl"] for t in tr), reverse=True)
    return dict(n=len(tr), total=sum(t["pnl"] for t in tr), pf=(g / l if l else float("inf")),
                mdd=mdd, worst=min(daily), worst_day=days[daily.index(min(daily))],
                win=100.0 * sum(1 for t in tr if t["pnl"] > 0) / max(len(tr), 1),
                worst_tr=min((t["pnl"] for t in tr), default=0.0),
                ex1=sum(t["pnl"] for t in tr) - sum(top[:1]),
                ex2=sum(t["pnl"] for t in tr) - sum(top[:2]),
                loss_days=sum(1 for x in daily if x < 0))


days = sorted(f.stem.split("_")[-1] for f in OUTD.glob("L85_A_2026*.json")
              if f.stem.split("_")[-1] >= FROM)
A, _ = load("L85_A", days)
B, LOG = load(TAG_B, days)
have = sorted(d for d in days if (OUTD / f"{TAG_B}_{d}.json").exists())
sa, sb = metrics(A, days), metrics(B, days)

print(f"A=현행   B=초기 30분 손절 2봉 확인   {len(days)}일 {days[0]}~{days[-1]}")
print(f"B 실제 재생 {len(have)}일: {' '.join(have)}\n")
print(f"{'':12s}{'A':>14s}{'B':>14s}{'Δ':>14s}")
for k, lab, f in (("n", "거래수", "{:14,.0f}"), ("total", "총손익", "{:+14,.0f}"),
                  ("pf", "PF", "{:14.3f}"), ("mdd", "MDD", "{:14,.0f}"),
                  ("worst", "최악일", "{:+14,.0f}"), ("worst_tr", "최악거래", "{:+14,.0f}"),
                  ("win", "승률%", "{:14.1f}"), ("loss_days", "손실일수", "{:14,.0f}"),
                  ("ex1", "상위1제외", "{:+14,.0f}"), ("ex2", "상위2제외", "{:+14,.0f}")):
    d = sb[k] - sa[k]
    print(f"{lab:12s}{f.format(sa[k])}{f.format(sb[k])}"
          f"{('{:+14.3f}' if k == 'pf' else '{:+14,.2f}').format(d)}")
print(f"  최악일자 A {sa['worst_day']}  B {sb['worst_day']}")

print("\n[유예 판정 로그]")
n_def = n_pass = n_hard = 0
for d in have:
    for x in LOG[d]:
        n_def += x["k"] == "DEFER"; n_pass += x["k"] == "PASS"; n_hard += x["k"] == "HARD"
        extra = (f" why={x['why']}" if x.get("why") else "") + \
                (f" held={x['held']}분" if x.get("held") is not None else "")
        print(f"  {d} {x['t'][11:19]} {x['k']:8s} {x['sym']} net={x.get('net')}{extra}")
print(f"  유예 {n_def}건 / 통과(대상 아님) {n_pass}건 / 유예 중 hard stop {n_hard}건")

print("\n[현행 손절 거래가 규칙 B 에서 어떻게 됐나]")
saved = worse = same = 0
tot = 0.0
for d in days:
    kb = {(t["t"], t["sym"]): t for t in B[d]}
    for t in A[d]:
        if "TIME_WINDOW_STOP_LOSS" not in t["why"]:
            continue
        m = kb.get((t["t"], t["sym"]))
        dl = (m["pnl"] if m else 0) - t["pnl"]
        tot += dl
        saved += dl > 0; worse += dl < 0; same += dl == 0
        print(f"  {d} {t['t']} {t['sym']} A {t['pnl']:+10,.0f} @{t['exit']} {t['exit_px']:>8,.0f}"
              f" -> B {(m['pnl'] if m else 0):+10,.0f} @{m['exit'] if m else '--':>8s}"
              f" {m['exit_px'] if m else 0:>8,.0f} ({m['why'][:24] if m else '진입없음'})"
              f"  Δ {dl:+10,.0f}")
print(f"  손절 {saved + worse + same}건 중 살림 {saved} / 악화 {worse} / 동일 {same}   Δ 합 {tot:+,.0f}")

print("\n[그 밖에 달라진 거래]")
other = 0.0
for d in days:
    ka = {(t["t"], t["sym"]): t for t in A[d]}
    kb = {(t["t"], t["sym"]): t for t in B[d]}
    for k in sorted(set(ka) | set(kb)):
        ra, rb = ka.get(k), kb.get(k)
        if ra and "TIME_WINDOW_STOP_LOSS" in ra["why"]:
            continue
        if ra and rb and abs(ra["pnl"] - rb["pnl"]) < 1 and ra["exit"] == rb["exit"]:
            continue
        dl = (rb["pnl"] if rb else 0) - (ra["pnl"] if ra else 0)
        other += dl
        fa = f'{ra["t"]} {ra["why"][:20]:20s} {ra["pnl"]:+10,.0f}' if ra else "-" * 20
        fb = f'{rb["t"]} {rb["why"][:20]:20s} {rb["pnl"]:+10,.0f}' if rb else "-" * 20
        print(f"  {d} {k[1]} | A {fa} | B {fb} | Δ {dl:+10,.0f}")
print(f"  2차 효과 Δ 합 {other:+,.0f}   (손절 {tot:+,.0f} + 2차 {other:+,.0f} = {tot + other:+,.0f})")
