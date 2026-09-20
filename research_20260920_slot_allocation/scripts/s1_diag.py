"""S1 — 슬롯 배분 진단: 3회 한도가 실제로 얼마나 묶여 있고, 천장은 어디인가.

세션 구획(production): 오전 = 09:00~11:00, 오후 = 11:00~14:50.
오전 3번째 진입은 quality 게이트, 오후 진입은 TEG 게이트, 오후 동일방향 2회차 금지.
"""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
import axlib as A
from common import summarize, compound

E1 = pickle.load(open(A.HERE / "e1.pkl", "rb"))
SOLO = {(r["date"], r["at"]): r for r in pickle.load(open(A.HERE / "e3.pkl", "rb")).values()}
FL = pd.DataFrame(E1["N1C1"])
TS = E1["trades_n1c1"]
D = E1["dates"]
T = pd.DataFrame([{"date": t["date"], "at": t["entry_time"], "net": float(t["net_pct"]),
                   "w1a": float(t["w1a"]), "slot": t["slot_number"], "session": t["session"],
                   "peak": float(t["peak_net_pct"]), "reason": t["exit_reason"],
                   "dir": t["direction"]} for t in TS])


def blk(t):
    print("\n" + "=" * 124); print(t); print("=" * 124)


blk("1. 하루 진입 횟수 분포 (78 영업일)")
cnt = T.groupby("date").size().reindex(D, fill_value=0)
for k in range(4):
    dd = cnt[cnt == k].index
    s = sum(float(x["net"]) * float(x["w1a"]) for _, x in T.iterrows() if x["date"] in set(dd))
    print(f"  {k}회 진입한 날 {len(dd):3d}일   그날들 가중손익합 {s:+8.2f}%p")

blk("2. 3회 한도가 실제로 묶인 날 (SLOT_CAP 거절이 발생한 날)")
capped = sorted(set(FL[FL["reason"] == "TW2_3SLOT_REJECT_DAILY_SLOT_CAP"]["date"]))
same_dir = sorted(set(FL[FL["reason"] == "TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND"]["date"]))
print(f"  SLOT_CAP 거절 발생일 {len(capped)}일 / 총 거절 {int((FL['reason']=='TW2_3SLOT_REJECT_DAILY_SLOT_CAP').sum())}건")
print(f"  오후 동일방향 2회차 거절 발생일 {len(same_dir)}일")
print(f"  3회를 다 쓴 날 {int((cnt==3).sum())}일 / 그중 추가 후보까지 있던 날 {len(set(capped)&set(cnt[cnt==3].index))}일")

blk("3. 슬롯 순번 / 세션별 실제 성과")
for c_ in ("slot", "session"):
    for k, g in sorted(T.groupby(c_)):
        v = g["net"]
        print(f"  {c_}={str(k):10s} n={len(g):4d} 합{v.sum():+8.2f} 평균{v.mean():+6.2f} "
              f"중앙{v.median():+6.2f} 승률{100*(v>0).mean():5.1f}% peak평균{g['peak'].mean():5.2f}")

blk("4. 진입 시각대별 실제 성과 (슬롯을 언제 쓰는가)")
T["hh"] = T["at"].str.slice(11, 13).astype(int)
for k, g in sorted(T.groupby("hh")):
    v = g["net"]
    print(f"  {k:02d}시   n={len(g):4d} 합{v.sum():+8.2f} 평균{v.mean():+6.2f} 중앙{v.median():+6.2f} "
          f"승률{100*(v>0).mean():5.1f}% peak평균{g['peak'].mean():5.2f}")

blk("5. 천장(오라클) — 그날 '슬롯만 있었다면 가능했던' 후보 중 solo 상위 3개를 골랐다면")
CAND_REASONS = {"TW_APPROVED", "TW2_3SLOT_REJECT_DAILY_SLOT_CAP",
                "TW2_3SLOT_REJECT_SAME_DIRECTION_AFTERNOON_2ND"}
rows = []
for d in D:
    g = FL[(FL["date"] == d) & (FL["approved"] | FL["reason"].isin(CAND_REASONS))]
    cand = []
    for _, r in g.iterrows():
        s = SOLO.get((r["date"], r["at"]))
        if s and s.get("solo_net") is not None:
            cand.append((float(s["solo_net"]), r["at"], r["approved"]))
    act = float(T[T["date"] == d]["net"].sum())
    n_act = int((T["date"] == d).sum())
    best = sorted(cand, reverse=True)[:3]
    rows.append({"date": d, "n_cand": len(cand), "n_act": n_act, "act_sum": act,
                 "oracle_sum": sum(x[0] for x in best),
                 "solo_act_sum": sum(x[0] for x in cand if x[2])})
O = pd.DataFrame(rows)
print(f"  후보가 4개 이상이던 날 {int((O['n_cand']>3).sum())}일 / 후보 총합 {int(O['n_cand'].sum())}")
print(f"  실제 단순합       {O['act_sum'].sum():+8.2f}%p")
print(f"  실제 진입의 solo합 {O['solo_act_sum'].sum():+8.2f}%p  (측정 편향 참고)")
print(f"  오라클 상위3 solo합 {O['oracle_sum'].sum():+8.2f}%p")
print(f"  천장 여유(오라클 - 실제solo) {O['oracle_sum'].sum()-O['solo_act_sum'].sum():+8.2f}%p")
X = O[O["n_cand"] > 3].sort_values("oracle_sum", ascending=False)
print(f"\n  후보 4개 이상이던 날 상세 (상위 15일):")
print(f"  {'일자':10s} {'후보':>4s} {'실제':>4s} {'실제합':>8s} {'실제solo':>9s} {'오라클':>8s} {'여유':>8s}")
for _, r in X.head(15).iterrows():
    print(f"  {r['date']:10s} {r['n_cand']:4d} {r['n_act']:4d} {r['act_sum']:+8.2f} "
          f"{r['solo_act_sum']:+9.2f} {r['oracle_sum']:+8.2f} {r['oracle_sum']-r['solo_act_sum']:+8.2f}")

blk("6. 후보 순서 vs 성과 — '먼저 온 것이 더 좋은가'")
seq = []
for d in D:
    g = FL[(FL["date"] == d) & (FL["approved"] | FL["reason"].isin(CAND_REASONS))].sort_values("at")
    for i, (_, r) in enumerate(g.iterrows(), 1):
        s = SOLO.get((r["date"], r["at"]))
        if s and s.get("solo_net") is not None:
            seq.append({"k": i, "solo": float(s["solo_net"]), "appr": bool(r["approved"]),
                        "tq": int(r["tq_score"]), "session": r["session"]})
Q = pd.DataFrame(seq)
for k, g in sorted(Q.groupby("k")):
    if len(g) < 3:
        continue
    print(f"  그날 {k}번째 후보  n={len(g):4d} solo합{g['solo'].sum():+8.2f} 평균{g['solo'].mean():+6.2f} "
          f"중앙{g['solo'].median():+6.2f} 승률{100*(g['solo']>0).mean():5.1f}%")
