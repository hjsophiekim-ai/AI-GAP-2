"""E1: N1 adaptive 래더가 실제로 어느 가지를 탔는가. READ-ONLY.

  추세 ok   : TP1 3.5 / 매도비중 0.0 / TP2 8.0   <- 사용자가 원하는 '길게'
  추세 아님 : TP1 3.0 / 매도비중 0.2 / TP2 4.0

가지는 거래원장에서 직접 읽는다 -- TP1_PARTIAL 레그의 수량비가 0 에 가까우면
추세 가지, 0.2 면 비추세 가지다.
"""
import glob, json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
REPO = r"G:/다른 컴퓨터/내 노트북 (2)/Desktop/AI-GAP 2"
ROOT = REPO + "/research_20261004_chop_staged"
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
O4 = REPO + "/research_20261002_sept_strategy_replay/round4_lockout/replay_json"
OUT = os.path.dirname(os.path.abspath(__file__))
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}; exec(compile(src, "an8lib", "exec"), ns)
trades = ns["trades"]
ap = lambda d: (f"{O3}/REAL_A_{d}.json" if os.path.exists(f"{O3}/REAL_A_{d}.json") else f"{O4}/REAL_A_{d}.json")

D = pd.read_csv(OUT + "/days.csv", dtype={"day": str}).set_index("day")
L = pd.read_csv(OUT + "/legs.csv", dtype={"day": str})

rows = []
for d in sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")}):
    o = json.load(open(ap(d), encoding="utf-8"))
    pair = float(D.loc[d, "pair_min_mfe"]) if d in D.index else 0.0
    g = L[L.day == d].sort_values("mins")
    for t in trades(o):
        rs = "+".join(dict.fromkeys(t["reasons"]))
        m = t["entry"].hour * 60 + t["entry"].minute
        # 그 진입 시점까지 당일 플래그(레그) 수 + 직전 레그 지속
        prior = g[g.mins <= m]
        nflag = len(prior)
        last_dur = float(prior.iloc[-1]["dur"]) if len(prior) else np.nan
        # TP1 레그 수량비로 가지 판정
        br, ratio = "미도달", np.nan
        if "TP1_PARTIAL" in rs:
            q1 = t["legs"][0][1]
            ratio = q1 / t["q"]
            br = "추세(0.0)" if ratio < 0.05 else ("비추세(0.2)" if ratio < 0.35 else f"기타({ratio:.2f})")
        rows.append(dict(day=d, pair=pair, at=t["entry"].strftime("%H:%M"), mins=m,
                         dir=t["dir"], regime=t.get("regime"), rs=rs, krw=t["krw"],
                         net=t["net"], hold=t["hold_min"], q=t["q"], px_in=t["px_in"],
                         branch=br, tp1_ratio=ratio, nflag=nflag, last_dur=last_dur))
T = pd.DataFrame(rows)
T.to_csv(OUT + "/branch.csv", index=False, encoding="utf-8")
print(f"# 거래 {len(T)}건 · TP1 도달 {int((T.branch!='미도달').sum())}건")
print("\n## 래더 가지별")
print(T.groupby("branch").agg(n=("krw", "size"), 손익=("krw", "sum"), 거래당=("krw", "mean"),
                              평균net=("net", "mean"), 보유분=("hold", "mean")).round(1).to_string())
print("\n## 가지 x 진입 regime (P3 분류)")
print(pd.crosstab(T.branch, T.regime.fillna("?")).to_string())
print("\n## 가지 x 오늘형 날")
T["big"] = np.where(T.pair >= 1.0, "오늘형", "그외")
print(pd.crosstab(T.branch, T.big).to_string())
print("\n## TP1 도달 거래의 청산사유 x 가지")
S = T[T.branch != "미도달"]
print(pd.crosstab(S.rs, S.branch).to_string())
print("\n## 비추세 가지(TP2 4.0) 로 끝난 거래 — 더 갈 수 있었나?")
print(S[S.branch == "비추세(0.2)"][["day", "at", "dir", "regime", "net", "hold", "krw", "rs"]]
      .sort_values("net", ascending=False).to_string(index=False))
