"""MU 상태별 분해 — N1 래더 11일 손절 16건 (research_20261102_shake_tolerance 재집계).

운영구성 58일 손절 20건 중 MU 1분봉이 있는 날(10/19~)에 걸린 것은 1건뿐이라
"MU CONFIRM 한정" 가설을 검정할 수 없다. N1 표본은 10/16~10/30 이라 16건 중
11건이 MU 창 안에 들어온다 -- 구성이 달라 production 결론은 못 되지만,
MU 상태가 유예의 성패를 가르는지 **방향**은 볼 수 있다.

V2 = 그 연구의 '3분봉 2개 연속 확인' (= 사용자 제안 2번과 같은 규칙, 30분 조건 없음).
"""
import sys
from pathlib import Path

import pandas as pd

sys.stdout.reconfigure(encoding="utf-8")
ROOT = Path(__file__).resolve().parents[2]
CF = ROOT / "research_20261102_shake_tolerance/out_cf.csv"
MU = ROOT / "research_20261104_mu_primary/out/mu_flags.csv"
EARLY_MIN = 30
DIR_OF = {"레버": "RED", "인버스": "BLUE"}

d = pd.read_csv(CF, encoding="utf-8-sig")
d["dV2"] = d.V2 - d.A
d["day"] = d.day.astype(int)
d["entry_t"] = pd.to_datetime(d.day.astype(str) + " " + d.entry, format="%Y%m%d %H:%M")
d["dir"] = d.sym.map(DIR_OF)
d["early"] = d.held_min <= EARLY_MIN

m = pd.read_csv(MU, encoding="utf-8-sig")
m["decide_t"] = pd.to_datetime(m.day.astype(str) + " " + m.decide, format="%Y%m%d %H:%M")
mu_days = set(m.day.astype(int))


def state(r):
    if r.day not in mu_days:
        return "NO_DATA"
    w = m[(m.day == r.day) & (m.dir == r["dir"]) & (m.decide_t <= r.entry_t)]
    return str(w.sort_values("decide_t").iloc[-1].mu_state) if len(w) else "UNMATCHED"


d["mu"] = d.apply(state, axis=1)
pd.set_option("display.width", 220)
print("N1(TREND 래더) 11일 10/16~10/30 · TIME_WINDOW_STOP_LOSS 16건")
print("V2 = 3분봉 2개 연속 확인 (30분 조건 없음). Δ = V2 − 현행\n")
print(d[["day", "entry", "stop", "sym", "held_min", "early", "mu", "A", "V2", "V2_why", "dV2"]]
      .to_string(index=False, float_format=lambda x: f"{x:,.0f}"))


def agg(sub, label):
    if not len(sub):
        print(f"  {label:30s}  0건")
        return
    x = sub.dV2
    top = sorted(x, reverse=True)
    print(f"  {label:30s} {len(sub):2d}건  Δ합 {x.sum():+10,.0f}  "
          f"개선 {int((x > 0).sum())} / 악화 {int((x < 0).sum())}  "
          f"최악Δ {x.min():+9,.0f}  상위1제외 {x.sum() - sum(top[:1]):+10,.0f}")


print("\n[부분집합]")
agg(d, "전체 16건")
agg(d[d.early], f"진입 후 {EARLY_MIN}분 이내")
agg(d[~d.early], f"{EARLY_MIN}분 초과")
print("\n[MU 상태별]  (MU 1분봉은 10/19 부터만 존재)")
for st in ["CONFIRM", "STALL", "DIVERGE", "UNAVAILABLE", "UNMATCHED", "NO_DATA"]:
    agg(d[d.mu == st], f"MU {st}")
print("\n[MU 상태별 × 30분 이내]")
for st in ["CONFIRM", "STALL", "DIVERGE", "UNAVAILABLE", "UNMATCHED", "NO_DATA"]:
    agg(d[(d.mu == st) & d.early], f"MU {st} & ≤{EARLY_MIN}분")
print("\n[사용자 제안대로 — 30분 이내 ∧ MU 가 BAD(DIVERGE) 아님]")
agg(d[d.early & d.mu.isin(["CONFIRM", "STALL"])], "CONFIRM+STALL & ≤30분")
agg(d[d.early & (d.mu == "CONFIRM")], "CONFIRM 만 & ≤30분")
