"""PEAK-LOCK 85영업일 분석 (2026-10-08). READ-ONLY.
A = E(UPFASTRS) 신규 재생 / B = PEAKLOCK / C = PEAKPART.
B/C 파일이 없는 날 = A 에서 tick peak >= 2.0 인 포지션이 없던 날 -> overlay 발동 불가 -> A 와 동일.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
W = os.path.dirname(os.path.abspath(__file__))
R = W + "/../wt"
ROOT = R + "/research_20261004_chop_staged"
O = W + "/out"
O3 = R + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
BRKD = R + "/research_20261003_breakout_confirm/wk/out6"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades, metrics = ns["trades"], ns["metrics"]
DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
assert len(DAYS) == 85

pb = lambda d: (f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json" if os.path.exists(f"{ROOT}/wk/out14/REAL_EARLYBRK15_{d}.json") else f"{BRKD}/REAL_BRK15_{d}.json")
pc = lambda d: (f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json" if os.path.exists(f"{ROOT}/wk/out15/REAL_EARLYUPFAST_{d}.json") else pb(d))
pe = lambda d: (f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json" if os.path.exists(f"{ROOT}/wk/out17/REAL_UPFASTRS_{d}.json") else pc(d))
ld = lambda p: json.load(open(p, encoding="utf-8"))
TAG = {"A": "UPFASTRS", "B": "PEAKLOCK", "C": "PEAKPART"}

J = {v: {} for v in "ABC"}
SRC = {v: {} for v in "BC"}
for d in DAYS:
    J["A"][d] = ld(f"{O}/REAL_UPFASTRS_{d}.json")
    for v in "BC":
        p = f"{O}/REAL_{TAG[v]}_{d}.json"
        SRC[v][d] = os.path.exists(p)
        J[v][d] = ld(p) if SRC[v][d] else J["A"][d]


def okey(o):
    return [(x["t"], x["side"], x["sym"], x["qty"], x["px"]) for x in o["orders"]]


# ── 무결성 ① : 신규 A == 기존 E 저장본(an22 의 pe 경로) 주문 단위
same = [d for d in DAYS if okey(J["A"][d]) == okey(ld(pe(d)))]
diff_days = [d for d in DAYS if d not in same]
anchor = sum(t["krw"] for d in DAYS for t in trades(ld(pe(d))))
print(f"# 무결성① 신규 A vs 기존 E 저장본: 주문 동일 {len(same)}/85일, 다른 날 {diff_days}")
print(f"#   기존 E 앵커 {anchor:+,.0f} (기준 +18,412,222)")

# ── 무결성 ② : 트리거 없는 날을 일부러 돌린 B/C 는 A 와 동일해야
peakmax = {d: max([e["peak"] for e in J["A"][d].get("peaks", [])] or [-99]) for d in DAYS}
trig = [d for d in DAYS if peakmax[d] >= 2.0]
for v in "BC":
    ctl = [d for d in DAYS if SRC[v][d] and peakmax[d] < 2.0]
    bad = [d for d in ctl if okey(J[v][d]) != okey(J["A"][d])]
    miss = [d for d in trig if not SRC[v][d]]
    print(f"# 무결성② {v}: 비트리거 대조일 {len(ctl)}일 중 불일치 {bad} / 트리거일 {len(trig)}일 중 미실행 {miss}")

T = {v: [] for v in "ABC"}
for v in "ABC":
    for d in DAYS:
        tr = trades(J[v][d])
        pk = J[v][d].get("peaks", [])
        for t in tr:
            c = [e for e in pk if ((e["sym"] == "0193T0") == t["dir"].startswith("UP"))
                 and t["entry"] <= pd.Timestamp(e["first"]) <= t["exit"] + pd.Timedelta(seconds=1)]
            t["peak"] = max([e["peak"] for e in c] or [np.nan])
            t["pl_exit"] = any("PEAK_LOCK_STOP" in r for r in t["reasons"])
            t["part25"] = any("PARTIAL25" in r for r in t["reasons"])
        T[v] += tr
M = {v: metrics(DAYS, T[v]) for v in "ABC"}
DA = {v: pd.Series({d: sum(t["krw"] for t in T[v] if t["day"] == d) for d in DAYS}) for v in "ABC"}

print("\n## 1. 85영업일 성과 (REAL 체결, 1,000만원 기준)\n")
print("| 지표 | A 기존 E | B PEAK-LOCK | C 25%+PEAK-LOCK | B−A | C−A |")
print("|---|---|---|---|---|---|")
rows = [("총손익", "krw", "{:+,.0f}"), ("복리%", "comp", "{:+.2f}"), ("PF", "pf", "{:.2f}"), ("MDD%", "mdd", "{:.2f}"),
        ("최대1일손실", "dmin", "{:+,.0f}"), ("최대1일수익", "dmax", "{:+,.0f}"), ("손실일", "lossd", "{:d}"),
        ("승률%", "win", "{:.1f}"), ("거래수", "n", "{:d}"), ("평균보유(분)", "hold", "{:.1f}")]
for nm, k, f in rows:
    a, b, c = M["A"][k], M["B"][k], M["C"][k]
    dfm = (lambda x: f"{x:+,.0f}") if k in ("krw", "dmin", "dmax") else (lambda x: f"{x:+d}") if k in ("lossd", "n") else (lambda x: f"{x:+.2f}")
    print(f"| {nm} | {f.format(a)} | {f.format(b)} | {f.format(c)} | {dfm(b-a)} | {dfm(c-a)} |")

# ── 거래 매칭 (day, 진입시각, 방향)
def key(t):
    return (t["day"], t["entry"].strftime("%H:%M:%S"), t["dir"])
IA = {key(t): t for t in T["A"]}
for v in "BC":
    IV = {key(t): t for t in T[v]}
    mk = [k for k in IA if k in IV]
    only_a = [k for k in IA if k not in IV]
    only_v = [k for k in IV if k not in IA]
    dm = sum(IV[k]["krw"] - IA[k]["krw"] for k in mk)
    print(f"\n## {v} 거래 매칭: 공통 {len(mk)} (차이 {dm:+,.0f}) / A만 {len(only_a)} ({sum(IA[k]['krw'] for k in only_a):+,.0f}) "
          f"/ {v}만 {len(only_v)} ({sum(IV[k]['krw'] for k in only_v):+,.0f})")
    for k in only_a:
        print(f"   A만 {k} {IA[k]['krw']:+,.0f} {ns['rs'](IA[k])}")
    for k in only_v:
        print(f"   {v}만 {k} {IV[k]['krw']:+,.0f} {ns['rs'](IV[k])}")

print("\n## 2. A 에서 peak 도달 후 반납한 거래 — 같은 거래의 B/C 결과\n")
print("| 그룹 (A 기준) | 건수 | A 손익 | B 손익 | B 방어 | C 손익 | C 방어 |")
print("|---|---|---|---|---|---|---|")
grp = [
    ("peak>=2% 전체", lambda t: t["peak"] >= 2.0),
    ("peak>=2% → 최종 <1% (반납)", lambda t: t["peak"] >= 2.0 and t["net"] < 1.0),
    ("peak>=2% → 최종 <0 (손실전환)", lambda t: t["peak"] >= 2.0 and t["net"] < 0.0),
    ("peak>=3% 전체", lambda t: t["peak"] >= 3.0),
    ("peak>=3% → 최종 <2% (반납)", lambda t: t["peak"] >= 3.0 and t["net"] < 2.0),
    ("peak>=4% → 최종 <2.5% (반납)", lambda t: t["peak"] >= 4.0 and t["net"] < 2.5),
    ("큰 승자 A net>=4%", lambda t: t["net"] >= 4.0),
    ("큰 승자 A net>=6%", lambda t: t["net"] >= 6.0),
]
IB = {key(t): t for t in T["B"]}
IC = {key(t): t for t in T["C"]}
for nm, f in grp:
    ks = [k for k, t in IA.items() if not np.isnan(t["peak"]) and f(t)] if "큰" not in nm else [k for k, t in IA.items() if f(t)]
    a = sum(IA[k]["krw"] for k in ks)
    b = sum(IB[k]["krw"] for k in ks if k in IB)
    c = sum(IC[k]["krw"] for k in ks if k in IC)
    nb = sum(1 for k in ks if k not in IB); nc = sum(1 for k in ks if k not in IC)
    note = f" (B미매칭 {nb}, C미매칭 {nc})" if nb or nc else ""
    print(f"| {nm}{note} | {len(ks)} | {a:+,.0f} | {b:+,.0f} | {b-a:+,.0f} | {c:+,.0f} | {c-a:+,.0f} |")

print("\n## 3. 큰 승자 — A 상위 거래 10건\n")
print("| 일자 | 진입 | A net% | A peak | A 손익 | B 손익 | B−A | C 손익 | C−A | A 청산 | B 청산 |")
print("|---|---|---|---|---|---|---|---|---|---|---|")
for k, t in sorted(IA.items(), key=lambda kv: -kv[1]["krw"])[:10]:
    b, c = IB.get(k), IC.get(k)
    print(f"| {k[0]} | {k[1][:5]} | {t['net']:+.2f} | {t['peak']:.2f} | {t['krw']:+,.0f} | "
          f"{(b['krw'] if b else float('nan')):+,.0f} | {((b['krw'] if b else 0)-t['krw']):+,.0f} | "
          f"{(c['krw'] if c else float('nan')):+,.0f} | {((c['krw'] if c else 0)-t['krw']):+,.0f} | {ns['rs'](t)} | {ns['rs'](b) if b else '-'} |")

for v in "BC":
    print(f"\n## 4-{v}. 바뀐 거래 상세 ({TAG[v]})\n")
    print("| 일자 | 진입 | 방향 | A peak | A net% | A 손익 | " + v + " net% | " + v + " 손익 | 차이 | A 청산 | " + v + " 청산 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    IV = IB if v == "B" else IC
    for k in sorted(IA):
        t, u = IA[k], IV.get(k)
        if u is None or abs(u["krw"] - t["krw"]) < 1:
            continue
        print(f"| {k[0]} | {k[1][:5]} | {t['dir'][:2]} | {t['peak']:.2f} | {t['net']:+.2f} | {t['krw']:+,.0f} | {u['net']:+.2f} | {u['krw']:+,.0f} | "
              f"{u['krw']-t['krw']:+,.0f} | {ns['rs'](t)} | {ns['rs'](u)} |")
    ev = [e for d in DAYS for e in J[v][d].get("pl", [])]
    print(f"\n  overlay 이벤트: PEAK_LOCK {sum(e['ev']=='PEAK_LOCK' for e in ev)}건, PART25 {sum(e['ev']=='PART25' for e in ev)}건, "
          f"PART25_SKIP {sum(e['ev']=='PART25_SKIP' for e in ev)}건")

print("\n## 5. 집중도 · 기간 분할\n")
print("| 항목 | B−A | C−A |")
print("|---|---|---|")
dd = {v: (DA[v] - DA["A"]) for v in "BC"}
print(f"| 합계 | {dd['B'].sum():+,.0f} | {dd['C'].sum():+,.0f} |")
print(f"| 바뀐 날 수 | {(dd['B'].abs()>=1).sum()} | {(dd['C'].abs()>=1).sum()} |")
print(f"| 개선/악화 일수 | {(dd['B']>=1).sum()}/{(dd['B']<=-1).sum()} | {(dd['C']>=1).sum()}/{(dd['C']<=-1).sum()} |")
for k in (1, 2, 3):
    sb = dd["B"].sort_values(ascending=False); sc = dd["C"].sort_values(ascending=False)
    print(f"| 상위 {k}일 제외 | {sb.sum()-sb.head(k).sum():+,.0f} | {sc.sum()-sc.head(k).sum():+,.0f} |")
print(f"| 최고 1일 | {dd['B'].max():+,.0f} ({dd['B'].idxmax()}) | {dd['C'].max():+,.0f} ({dd['C'].idxmax()}) |")
print(f"| 최악 1일 | {dd['B'].min():+,.0f} ({dd['B'].idxmin()}) | {dd['C'].min():+,.0f} ({dd['C'].idxmin()}) |")
# 원 성과(일손익 기준) 상위 1~3일 제외 — 각 변형에서 자기 상위일 제외
print("\n| 각 변형 총손익에서 자기 상위 N일 제외 | A | B | C |")
print("|---|---|---|---|")
for k in (1, 2, 3):
    vals = [DA[v].sum() - DA[v].sort_values(ascending=False).head(k).sum() for v in "ABC"]
    print(f"| 상위 {k}일 제외 | {vals[0]:+,.0f} | {vals[1]:+,.0f} | {vals[2]:+,.0f} |")

seg = pd.Series(np.where(np.array(DAYS) < "20260801", "train 5~7월", "test 8~10월"), index=DAYS)
print("\n| 구간 | 일수 | A | B | B−A | C | C−A |")
print("|---|---|---|---|---|---|---|")
for s_ in ("train 5~7월", "test 8~10월"):
    ix = seg[seg == s_].index
    a, b, c = DA["A"][ix].sum(), DA["B"][ix].sum(), DA["C"][ix].sum()
    print(f"| {s_} | {len(ix)} | {a:+,.0f} | {b:+,.0f} | {b-a:+,.0f} | {c:+,.0f} | {c-a:+,.0f} |")
for s_ in ("train 5~7월", "test 8~10월"):
    ix = list(seg[seg == s_].index)
    mm = {v: metrics(ix, [t for t in T[v] if t["day"] in ix]) for v in "ABC"}
    print(f"  {s_}: PF A {mm['A']['pf']:.2f} / B {mm['B']['pf']:.2f} / C {mm['C']['pf']:.2f} · "
          f"MDD A {mm['A']['mdd']:.2f} / B {mm['B']['mdd']:.2f} / C {mm['C']['mdd']:.2f} · "
          f"손실일 A {mm['A']['lossd']} / B {mm['B']['lossd']} / C {mm['C']['lossd']}")

# 부트스트랩 (일 단위) P(diff>0)
rng = np.random.default_rng(0)
for v in "BC":
    x = dd[v].to_numpy()
    bs = rng.choice(x, size=(20000, len(x)), replace=True).sum(1)
    print(f"  부트스트랩 P({v}−A>0) = {(bs>0).mean()*100:.1f}%")
pd.DataFrame({"day": DAYS, "A": DA["A"].values, "B": DA["B"].values, "C": DA["C"].values}).to_csv(W + "/daily.csv", index=False)
