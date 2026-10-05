"""사전 스크리닝: '오전 + C1 chop' 플래그에만 돌파확인을 걸면 어떻게 되나.
기존 BREAKOUT30-1T / BREAKOUT15 재생 결과(2026-10-03)에서 해당 부분집합만 떼어낸다.
새 replay 없음. 1차 근사(슬롯 연쇄 미반영). READ-ONLY.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
BRK = REPO + "/research_20261003_breakout_confirm/wk/out6"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]

C = pd.read_csv(ROOT + "/calib2_entries.csv")
C["day"] = C["day"].astype(str)
C["key"] = C["day"] + " " + pd.to_datetime(C["t"]).dt.strftime("%H:%M:%S")
C["c1"] = (C.pe < 0.20) & (C.rng < 1.0) & (C.xc >= 3)
C["am"] = pd.to_datetime(C["t"]).dt.hour < 12
CM = C.set_index("key")

DAYS = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
A = {}
for d in DAYS:
    for t in trades(json.load(open(f"{O3}/REAL_A_{d}.json", encoding="utf-8"))):
        A[t["base"]] = t

for TAG in ("BRK30", "BRK15"):
    print(f"\n{'='*70}\n## {TAG} 결과에서 '오전 ∧ C1 chop' 부분집합만 추출")
    ev, V = [], {}
    for d in DAYS:
        p = f"{BRK}/REAL_{TAG}_{d}.json"
        if not os.path.exists(p):
            continue
        o = json.load(open(p, encoding="utf-8"))
        ev += (o.get("brk") or [])
        for t in trades(o):
            V[t["base"].replace(":BRK", "")] = t
    armed = [e for e in ev if e["ev"] == "ARMED"]
    fired = {e["sid"] for e in ev if e["ev"] == "FIRED" and e.get("executed")}
    rows = []
    for e in armed:
        sid = e["sid"]
        a = A.get(sid)
        if a is None:
            continue
        key = a["day"] + " " + a["entry"].strftime("%H:%M:%S")
        if key not in CM.index:
            continue
        r = CM.loc[key]
        if isinstance(r, pd.DataFrame):
            r = r.iloc[0]
        rows.append(dict(sid=sid, day=a["day"], t=a["entry"], am=bool(r["am"]), c1=bool(r["c1"]),
                         pe=r["pe"], rng=r["rng"], xc=int(r["xc"]), a_krw=a["krw"], a_net=a["net"],
                         fired=(sid in fired), b_krw=(V[sid]["krw"] if sid in V else 0.0),
                         a_rs="+".join(dict.fromkeys(a["reasons"]))))
    df = pd.DataFrame(rows)
    sub = df[df.am & df.c1]
    print(f"  전체 ARMED {len(df)}건 중 오전∧chop **{len(sub)}건** "
          f"(오전만 {int((df.am).sum())} · chop만 {int((df.c1).sum())})")
    if not len(sub):
        continue
    canc = sub[~sub.fired]
    fire = sub[sub.fired]
    print(f"  - 미도달 폐기 {len(canc)}건: A 손익 {canc.a_krw.sum():+,.0f}원 "
          f"(손실 {int((canc.a_krw<0).sum())}건 {canc[canc.a_krw<0].a_krw.sum():+,.0f} / "
          f"수익 {int((canc.a_krw>0).sum())}건 {canc[canc.a_krw>0].a_krw.sum():+,.0f})")
    print(f"  - 돌파 진입 {len(fire)}건: A {fire.a_krw.sum():+,.0f} → {TAG} {fire.b_krw.sum():+,.0f} "
          f"(Δ {fire.b_krw.sum()-fire.a_krw.sum():+,.0f})")
    net = (-canc.a_krw.sum()) + (fire.b_krw.sum() - fire.a_krw.sum())
    print(f"  => **부분집합 순효과 {net:+,.0f}원** (폐기이익 {-canc.a_krw.sum():+,.0f} + 지연손상 {fire.b_krw.sum()-fire.a_krw.sum():+,.0f})")
    print(f"\n  거래별 내역")
    for _, r in sub.sort_values("t").iterrows():
        print(f"    {r['day']} {r['t']:%H:%M} PE{r['pe']:.2f} RNG{r['rng']:.2f} XC{r['xc']} "
              f"{'돌파진입' if r['fired'] else '**폐기**'} | A {r['a_rs']} {r['a_krw']:+,.0f}"
              + (f" → {TAG} {r['b_krw']:+,.0f} (Δ {r['b_krw']-r['a_krw']:+,.0f})" if r['fired'] else ""))
    # 비교: 같은 규칙을 오전 전체 / chop 전체에 걸었을 때
    for lab, m in (("오전 전체", df.am), ("chop 전체(종일)", df.c1)):
        z = df[m]
        c2, f2 = z[~z.fired], z[z.fired]
        n2 = (-c2.a_krw.sum()) + (f2.b_krw.sum() - f2.a_krw.sum())
        print(f"  (대조) {lab}: {len(z)}건 · 순효과 {n2:+,.0f}원")
