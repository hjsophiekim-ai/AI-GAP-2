"""사전 지정한 조합만 평가한다 (grid search 아님 — 전부 production 에 이미 있는 정의).

  B_USER  사용자 규칙 B : MU CONFIRM 진입 / DIVERGE 차단 / STALL 은 체결강도 ∧ 반대gap<=400
  S0      MU CONFIRM
  S1      S0 ∧ 체결강도 MATCH                     (exec_strength, 기준 100 고정)
  S2      = production XS 의 STRONG 정의 그대로 : MU CONFIRM ∧ 체결강도 MATCH ∧ HY gap EXPAND
  S3      S2 ∧ 반대방향 HY gap <= 400             (사용자 규칙 ②)
  S4      S2 ∧ breakout 아님 ∧ major score < 45   (sideways_filter v5 게이트 그대로)
  S5      S2 ∧ VWAP 우위                          (TEG 조건6 / TW2 VWAP veto 와 같은 계산)

슬롯 근사: 하루 시간순으로 훑으며 보유 30분이 겹치지 않는 것만, 최대 2건/일 (현행 CHOP 수준).
"""
import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.stdout.reconfigure(encoding="utf-8")
OUT = ROOT / "research_20261104_mu_primary" / "out"

d = pd.read_csv(OUT / "mu_flags.csv")
t = d[d["tradable"] & d["entry_px"].notna()].copy()
t["min"] = t["decide"].str.slice(0, 2).astype(int) * 60 + t["decide"].str.slice(3, 5).astype(int)
t["xs_ok"] = t["xs"] == "MATCH"
t["opp_le400"] = t["hy_gap_opp"] <= 400
t["expand"] = t["hy_gap_expand"] == True        # noqa: E712
t["nobreak"] = t["hy_breakout"] == False        # noqa: E712
t["sc45"] = t["hy_score"] < 45
t["vwap"] = t["hy_vwap_ok"] == True             # noqa: E712
C, S, D = t["mu_state"] == "CONFIRM", t["mu_state"] == "STALL", t["mu_state"] == "DIVERGE"

SETS = {
    "B_USER": C | (S & t["xs_ok"] & t["opp_le400"]),
    "S0 CONFIRM": C,
    "S1 +체결강도": C & t["xs_ok"],
    "S2 STRONG(XS정의)": C & t["xs_ok"] & t["expand"],
    "S3 STRONG+반대gap<=400": C & t["xs_ok"] & t["expand"] & t["opp_le400"],
    "S4 STRONG+sideways게이트": C & t["xs_ok"] & t["expand"] & t["nobreak"] & t["sc45"],
    "S5 STRONG+VWAP": C & t["xs_ok"] & t["expand"] & t["vwap"],
}
DAYS = sorted(t["day"].unique())


def slots(sub, cap=2, hold=30):
    keep, busy = [], -10 ** 9
    for _, r in sub.sort_values(["day", "min"]).iterrows():
        if r["day"] != (keep[-1]["day"] if keep else None):
            busy, n = -10 ** 9, 0
        n = sum(1 for k in keep if k["day"] == r["day"])
        if n >= cap or r["min"] < busy:
            continue
        busy = r["min"] + hold
        keep.append(r)
    return pd.DataFrame(keep)


def stat(sub):
    if not len(sub):
        return "        0        -        -        -        -"
    return (f"{len(sub):9d} {sub['r30'].mean():+8.3f} {sub['r_eod'].mean():+8.3f} "
            f"{100 * (sub['path'] == 'TP1').mean():7.1f}% {len(sub) / len(DAYS):7.1f}")


print(f"MU 플래그 거래가능 {len(t)}건 / {len(DAYS)}영업일 (2026-10-19~10-30)\n")
print(f"{'조합':28s}{'n':>9s}{'r30평균':>9s}{'종가평균':>9s}{'TP1선':>8s}{'건/일':>8s}   |  "
      f"{'슬롯근사 n':>10s}{'r30평균':>9s}{'종가평균':>9s}{'TP1선':>8s}")
for name, mask in SETS.items():
    sub = t[mask]
    sl = slots(sub)
    print(f"{name:28s}{stat(sub)}   |  {stat(sl)}")

print(f"\n{'=' * 110}\nS2 STRONG 일자별 (슬롯 근사 전)\n{'=' * 110}")
s2 = t[SETS["S2 STRONG(XS정의)"]]
g = s2.groupby("day").agg(n=("r30", "size"), r30=("r30", "mean"), eod=("r_eod", "mean"),
                          tp1=("path", lambda x: (x == "TP1").sum()))
print(g.round(3).to_string())
print(f"  r30 이 양수인 날 {int((g['r30'] > 0).sum())}/{len(g)}  ·  "
      f"플래그 없는 날 {len(DAYS) - len(g)}")

print(f"\n{'=' * 110}\nS2 STRONG 전수 (슬롯 근사 적용분은 *)\n{'=' * 110}")
kept = set(zip(slots(s2)["day"], slots(s2)["decide"])) if len(s2) else set()
for _, r in s2.sort_values(["day", "min"]).iterrows():
    mark = "*" if (r["day"], r["decide"]) in kept else " "
    print(f" {mark}{r['day']} {r['flag_end']}->{r['decide']} {r['dir']:4s} "
          f"cttr {r['cttr']:7.2f}  gap {r['hy_gap_same']:9.1f} opp {r['hy_gap_opp']:7.1f} "
          f"score {r['hy_score'] if pd.notna(r['hy_score']) else -1:5.1f}  "
          f"r30 {r['r30']:+7.3f}  eod {r['r_eod']:+7.3f}  {r['path']:7s} "
          f"mfe {r['mfe30']:+6.2f} mae {r['mae30']:+6.2f}")
