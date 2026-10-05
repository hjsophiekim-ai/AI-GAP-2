"""RS 보정표 생성 — 각 영업일마다 '그 날 이전' 진입들만으로 z통계와 상위20% 임계를 만든다.
미래정보 없음. 후보 간 비교가능성을 위해 A 의 진입집합을 고정 기준으로 쓴다. READ-ONLY.
"""
import json, os, sys
import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = os.path.dirname(os.path.abspath(__file__))
F = pd.read_csv(ROOT + "/score_features.csv")
F["day"] = F["day"].astype(str)
COLS = ["atr60", "min_open", "xc30"]
SGN = {"atr60": 1.0, "min_open": -1.0, "xc30": 1.0}
MIN_N = 30
DAYS = sorted(F.day.unique())
tbl = {}
for d in DAYS:
    past = F[F.day < d]
    if len(past) < MIN_N:
        continue
    mu = {c: float(past[c].mean()) for c in COLS}
    sd = {c: float(past[c].std() or 1.0) for c in COLS}
    rs = sum(SGN[c] * (past[c] - mu[c]) / sd[c] for c in COLS)
    tbl[d] = dict(mu=mu, sd=sd, thr=float(np.quantile(rs, 0.80)), n=int(len(past)))
json.dump(tbl, open(ROOT + "/rs_table.json", "w"), ensure_ascii=False, indent=1)
print(f"# 보정표 {len(tbl)}일 (활성 시작 {min(tbl)} · 과거표본 최소 {MIN_N}건)")

# 각 후보가 A 와 갈라질 수 있는 날
rows = []
for _, r in F.iterrows():
    d = r["day"]
    t = tbl.get(d)
    if t is None:
        rows.append((d, False)); continue
    rs = sum(SGN[c] * (r[c] - t["mu"][c]) / t["sd"][c] for c in COLS)
    rows.append((d, rs >= t["thr"]))
F["rs_hit"] = [x[1] for x in rows]
hitdays = sorted(F[F.rs_hit].day.unique())
print(f"# RS 상위20% 발동 {int(F.rs_hit.sum())}건 / {len(F)}건 · {len(hitdays)}일")

# 슬롯3 보유일
import glob
REPO = os.path.dirname(ROOT)
O3 = REPO + "/research_20261002_sept_strategy_replay/round3_85d/replay_json"
src = open(ROOT + "/an8.py", encoding="utf-8").read().split("DAYS = sorted(")[0]
ns = {"__file__": ROOT + "/an8.py"}
exec(compile(src, "an8lib", "exec"), ns)
ALLD = sorted({os.path.basename(p)[7:15] for p in glob.glob(O3 + "/REAL_A_*.json")})
slot3 = []
for d in ALLD:
    if len(ns["trades"](json.load(open(f"{O3}/REAL_A_{d}.json", encoding="utf-8")))) >= 3:
        slot3.append(d)
print(f"# 3번째 진입이 있는 날 {len(slot3)}일")

PREV = {}
for l in open(REPO + "/research_20261002_sept_strategy_replay/round3_85d/jobs4.txt"):
    p = l.split()
    if len(p) >= 5 and p[1] == "A":
        PREV[p[2]] = (p[3], p[4])
known = set(ALLD)


def need(flag):
    return [d for d in ALLD if d in flag or PREV.get(d, ("", ""))[0] in flag or PREV.get(d, ("", ""))[0] not in known]


nRS, nS3 = need(set(hitdays)), need(set(slot3))
nBoth = sorted(set(nRS) | set(nS3))
print(f"\n# 실행 필요일 — RS 후보 {len(nRS)}일 / SLOT3 후보 {len(nS3)}일")
print(f"# 작업량: RS125 {len(nRS)} + RS150 {len(nRS)} + SLOT3 {len(nS3)} + RS150CUT {len(nRS)} = "
      f"{len(nRS)*3 + len(nS3)}건")
json.dump({"rs_days": nRS, "slot3_days": nS3, "hitdays": hitdays, "slot3": slot3,
           "prev": PREV}, open(ROOT + "/rs_days.json", "w"), ensure_ascii=False)
# C1 왕복장(감액 대상) 과 RS 상위20% 의 겹침
C = pd.read_csv(ROOT + "/calib2_entries.csv")
C["key"] = C["day"].astype(str) + " " + pd.to_datetime(C["t"]).dt.strftime("%H:%M:%S")
F["key"] = F["day"] + " " + pd.to_datetime(F["entry"]).dt.strftime("%H:%M:%S")
m = F.merge(C[["key", "pe", "rng", "xc"]], on="key", how="left")
m["c1"] = (m.pe < 0.20) & (m.rng < 1.0) & (m.xc >= 3)
print(f"\n# 겹침 — RS 상위20% {int(m.rs_hit.sum())}건 · C1 왕복장 {int(m.c1.sum())}건 · "
      f"둘 다 {int((m.rs_hit & m.c1).sum())}건")
print(f"  RS 상위20% 손익 {m[m.rs_hit].krw.sum():+,.0f} · C1 손익 {m[m.c1].krw.sum():+,.0f} "
      f"· 둘 다 {m[m.rs_hit & m.c1].krw.sum():+,.0f}")
