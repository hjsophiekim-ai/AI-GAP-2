"""AN22: SLOW-TP2 재생결과를 85영업일 포트폴리오로 환산. READ-ONLY."""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
BRKD = REPO + "/research_20261003_breakout_confirm/wk/out6"
W = os.path.dirname(os.path.abspath(__file__)); O = W + "/wk21"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")
pb = lambda d: (f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json" if os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json") else f"{BRKD}/REAL_BRK15_{d}.json")
pc = lambda d: (f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json" if os.path.exists(f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json") else pb(d))
pe = lambda d: (f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json" if os.path.exists(f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json") else pc(d))
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
NEW = sorted({os.path.basename(p)[len("REAL_SLOWTP2_"):-5] for p in glob.glob(O + "/REAL_SLOWTP2_*.json")})

e_d, s_d, a_d, T = {}, {}, {}, {"E": [], "S": []}
for d in DAYS:
    te = trades(json.load(open(pe(d), encoding="utf-8")))
    e_d[d] = sum(t["krw"] for t in te); T["E"] += te
    a_d[d] = sum(t["krw"] for t in trades(json.load(open(ap(d), encoding="utf-8"))))
    if d in NEW:
        ts = trades(json.load(open(f"{O}/REAL_SLOWTP2_{d}.json", encoding="utf-8")))
    else:
        ts = te
    s_d[d] = sum(t["krw"] for t in ts); T["S"] += ts


def met(dd):
    a = np.array([dd[d] for d in DAYS], float)
    r = a / 10_000_000 * 100
    eq = np.cumprod(1 + r / 100)
    return dict(krw=a.sum(), comp=(eq[-1] - 1) * 100,
                mdd=(eq / np.maximum.accumulate(np.r_[1.0, eq])[1:] - 1).min() * 100,
                worst=a.min(), lossd=int((a < 0).sum()), best=a.max())


mE, mS, mA = met(e_d), met(s_d), met(a_d)
kE = np.array([t["krw"] for t in T["E"]], float); kS = np.array([t["krw"] for t in T["S"]], float)
pf = lambda k: k[k > 0].sum() / max(-k[k < 0].sum(), 1.0)
print(f"# 85영업일 (앵커 A {mA['krw']:+,.0f} / 기준 +17,227,606 · E {mE['krw']:+,.0f} / 기준 +18,412,222)")
ok = (round(mA['krw']) == 17_227_606 and round(mE['krw']) == 18_412_222)
print(f"# 앵커 검증: {'PASS' if ok else '## FAIL'}")
print("\n| 지표 | A 현행 N1/P3 | E 현행 | **E + SLOW-TP2** | E 대비 |")
print("|---|---|---|---|---|")
print(f"| 총손익 | {mA['krw']:+,.0f} | {mE['krw']:+,.0f} | **{mS['krw']:+,.0f}** | **{mS['krw']-mE['krw']:+,.0f}** |")
print(f"| 복리% | {mA['comp']:+.2f} | {mE['comp']:+.2f} | **{mS['comp']:+.2f}** | **{mS['comp']-mE['comp']:+.2f}** |")
print(f"| MDD% | {mA['mdd']:.2f} | {mE['mdd']:.2f} | **{mS['mdd']:.2f}** | {mS['mdd']-mE['mdd']:+.2f} |")
print(f"| PF | - | {pf(kE):.2f} | **{pf(kS):.2f}** | {pf(kS)-pf(kE):+.2f} |")
print(f"| 최대1일손실 | {mA['worst']:+,.0f} | {mE['worst']:+,.0f} | **{mS['worst']:+,.0f}** | {mS['worst']-mE['worst']:+,.0f} |")
print(f"| 최대1일수익 | {mA['best']:+,.0f} | {mE['best']:+,.0f} | **{mS['best']:+,.0f}** | {mS['best']-mE['best']:+,.0f} |")
print(f"| 손실일 | {mA['lossd']} | {mE['lossd']} | **{mS['lossd']}** | {mS['lossd']-mE['lossd']:+d} |")
print(f"| 승률% | - | {(kE>0).mean()*100:.1f} | **{(kS>0).mean()*100:.1f}** | {((kS>0).mean()-(kE>0).mean())*100:+.1f} |")
print(f"| 거래수 | - | {len(kE)} | {len(kS)} | {len(kS)-len(kE):+d} |")
print(f"| 복리/\|MDD\| | {mA['comp']/abs(mA['mdd']):.1f} | {mE['comp']/abs(mE['mdd']):.1f} | **{mS['comp']/abs(mS['mdd']):.1f}** | |")

D = pd.DataFrame({"day": DAYS, "E": [e_d[d] for d in DAYS], "S": [s_d[d] for d in DAYS]})
D["d"] = D.S - D.E
ch = D[D.d != 0].sort_values("d", ascending=False)
print(f"\n## 집중도 — 바뀐 {len(ch)}일 합 {ch.d.sum():+,.0f}")
for k in (1, 2, 3):
    print(f"  상위 {k}일 제외 -> {ch.d.sum()-ch.d.head(k).sum():+,.0f}")
print(f"  최악 1일 {ch.d.min():+,.0f}")
print("\n## 기간 분할")
D["seg"] = np.where(D.day < "20260801", "train 5~7월", "test 8~10월")
print(D.groupby("seg").agg(일수=("day", "size"), E=("E", "sum"), SLOW=("S", "sum"), 차이=("d", "sum")).round(0).to_string())
D.to_csv(W + "/an22_daily.csv", index=False, encoding="utf-8")
