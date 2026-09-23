"""Y81 — 80일 결과 강건성 분석 + BASE 대비 diff 전량. 엔진 재실행 없음."""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from common import compound, summarize

Z = pickle.load(open(HERE / "y80.pkl", "rb"))
DATES = Z["dates"]
V = {"BASE": Z["base"], "X1-A": Z["x1"], "FE만": Z["fe"], "AR1만": Z["ar"]}
D30 = DATES[-30:]
H1, H2 = DATES[:40], DATES[40:]
print("창: %s~%s (%d일) · 30일: %s~%s · BASE 재현성: %s"
      % (DATES[0], DATES[-1], len(DATES), D30[0], D30[-1],
         "일치" if Z["base_repro"] else "불일치!!"))

pd.set_option("display.width", 260)

# ── 1. 기간별 요약 ─────────────────────────────────────────────────────────
for label, ds in (("80일 전체", DATES), ("최근 30일", D30),
                  ("전반 40일", H1), ("후반 40일", H2)):
    rows = []
    for k, ts in V.items():
        m = summarize(ts, ds)
        rows.append(dict(변형=k, 거래=m["trades"], 복리=m["compound_pct"], PF=m["pf"],
                         MDD=m["mdd_pct"], 승률=m["win_rate_pct"], 월=m["monthly_pct"],
                         단순합=m["simple_pct"],
                         top5제외=m["top5_excl_pct"], top10제외=m["top10_excl_pct"],
                         최고일제외=m["excl_top1d"], 상위3일제외=m["excl_top3d"]))
    R = pd.DataFrame(rows)
    b = R[R.변형 == "BASE"].iloc[0]
    R["복리Δ"] = R.복리 - b.복리
    R["top5제외Δ"] = R.top5제외 - b.top5제외
    R["최고일제외Δ"] = R.최고일제외 - b.최고일제외
    print("\n" + "=" * 118)
    print("【%s】 %s ~ %s (%d일)" % (label, ds[0], ds[-1], len(ds)))
    print("=" * 118)
    print(R.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))

# ── 2. Top N 거래 제거 ─────────────────────────────────────────────────────
print("\n" + "=" * 118)
print("【Top N 거래 제거 후 복리 (80일)】 — 상위거래 의존도")
print("=" * 118)
rows = []
for k, ts in V.items():
    df = pd.DataFrame([t for t in ts if t["date"] in set(DATES)])
    df["pnl"] = df["net_pct"] * df["w1a"]
    r = {"변형": k}
    for n in (0, 1, 3, 5):
        top = df.nlargest(n, "pnl").index if n else []
        rest = df.drop(top).sort_values("exit_time")
        r["top%d제외" % n] = float(((1 + rest.pnl / 100).cumprod().iloc[-1] - 1) * 100)
    rows.append(r)
T = pd.DataFrame(rows)
bb = T[T.변형 == "BASE"].iloc[0]
for n in (0, 1, 3, 5):
    T["Δtop%d" % n] = T["top%d제외" % n] - bb["top%d제외" % n]
print(T.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))

# ── 3. LOO / bootstrap ─────────────────────────────────────────────────────
print("\n" + "=" * 118)
print("【LOO (하루씩 제외) · bootstrap (일 단위 복원추출 2000회)】 — BASE 대비 복리 델타")
print("=" * 118)
rng = np.random.default_rng(11)
for k in ("X1-A", "FE만", "AR1만"):
    loo = np.array([compound(V[k], [x for x in DATES if x != d])
                    - compound(V["BASE"], [x for x in DATES if x != d]) for d in DATES])
    bs = []
    for _ in range(2000):
        s = list(rng.choice(DATES, len(DATES), replace=True))
        bs.append(compound(V[k], s) - compound(V["BASE"], s))
    bs = np.array(bs)
    print("  %-6s LOO min %+8.3f / med %+8.3f / max %+8.3f · 델타<=0 인 날 %d/%d"
          % (k, loo.min(), np.median(loo), loo.max(), int((loo <= 0).sum()), len(loo)))
    print("         bootstrap mean %+8.3f  95%%CI [%+8.3f, %+8.3f]  P(>0)=%.1f%%"
          % (bs.mean(), np.percentile(bs, 2.5), np.percentile(bs, 97.5), (bs > 0).mean() * 100))

# ── 4. 월별 ────────────────────────────────────────────────────────────────
print("\n" + "=" * 118)
print("【월별 손익 (net_pct x w1a 합)】")
print("=" * 118)
mo = {}
for k, ts in V.items():
    df = pd.DataFrame([t for t in ts if t["date"] in set(DATES)])
    df["pnl"] = df["net_pct"] * df["w1a"]
    df["m"] = df["date"].str[:6]
    mo[k] = df.groupby("m")["pnl"].sum()
M = pd.DataFrame(mo).fillna(0.0)
M["X1-A Δ"] = M["X1-A"] - M["BASE"]
print(M.to_string(float_format=lambda v: f"{v:,.3f}"))

# ── 5. BASE 대비 entry/exit diff 전량 ──────────────────────────────────────
def key(t):
    return (t["date"], str(t["entry_time"]), t["direction"])


print("\n" + "=" * 118)
print("【BASE 대비 거래 diff 전량】")
print("=" * 118)
b = {key(t): t for t in V["BASE"]}
for k in ("X1-A", "FE만", "AR1만"):
    v = {key(t): t for t in V[k]}
    add = [v[x] for x in v if x not in b]
    rem = [b[x] for x in b if x not in v]
    chg = [(b[x], v[x]) for x in v if x in b
           and (str(b[x]["exit_time"]) != str(v[x]["exit_time"])
                or round(b[x]["net_pct"], 6) != round(v[x]["net_pct"], 6))]
    print("\n── %s : 추가 %d / 삭제 %d / 청산변경 %d ──" % (k, len(add), len(rem), len(chg)))
    if add:
        print(pd.DataFrame([dict(date=t["date"], entry=pd.Timestamp(t["entry_time"]).strftime("%H:%M"),
                                 dir=t["direction"][:1] + t["direction"][3:4], slot=t["slot_number"],
                                 exit=pd.Timestamp(t["exit_time"]).strftime("%m-%d %H:%M"),
                                 reason=t["exit_reason"], net=t["net_pct"], w1a=t["w1a"])
                            for t in add]).to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
        print("   추가분 net합 %+.3f%%p" % sum(t["net_pct"] * t["w1a"] for t in add))
    if rem:
        print("   삭제:", [(t["date"], pd.Timestamp(t["entry_time"]).strftime("%H:%M"),
                          round(t["net_pct"], 3)) for t in rem])
    if chg:
        C = pd.DataFrame([dict(date=o["date"], entry=pd.Timestamp(o["entry_time"]).strftime("%H:%M"),
                               dir=o["direction"][:1] + o["direction"][3:4],
                               base_exit=pd.Timestamp(o["exit_time"]).strftime("%H:%M"),
                               base_reason=o["exit_reason"], base_net=o["net_pct"],
                               x1_exit=pd.Timestamp(n["exit_time"]).strftime("%H:%M"),
                               x1_reason=n["exit_reason"], x1_net=n["net_pct"],
                               uplift=n["net_pct"] - o["net_pct"], w1a=o["w1a"])
                          for o, n in chg])
        print(C.to_string(index=False, float_format=lambda v: f"{v:,.3f}"))
        print("   청산변경 uplift 합 %+.3f%%p (가중 %+.3f%%p) · 개선 %d / 악화 %d"
              % (C.uplift.sum(), (C.uplift * C.w1a).sum(),
                 int((C.uplift > 0).sum()), int((C.uplift < 0).sum())))
