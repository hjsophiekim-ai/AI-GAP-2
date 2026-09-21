# -*- coding: utf-8 -*-
"""§14 스트레스 · §12 placebo · §11 LOO — 후보 F(floor 1.25). READ-ONLY."""
import sys, pickle, itertools, statistics as st
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P
from e5_cmp import SZ, BASE_K, D, A, A78, rows, RUNS
from app.trading.macd2.worker import _net_return_pct

FK = "F  floor 1.25 (더빨리)"
rng = np.random.default_rng(20260921)
rA, rF = rows(RUNS[BASE_K]), rows(RUNS[FK])
am = {(t["date"], t["entry_time"]): t for t in rA}
fm = {(t["date"], t["entry_time"]): t for t in rF}
CHG = [k for k in am if abs(am[k]["net_pct"] - fm[k]["net_pct"]) > 1e-9]
print(f"후보 F: 변경 거래 {len(CHG)}건\n")


def stressed(src, slip=0.0, delay=0):
    out = []
    for t in src:
        s = dict(t)
        net = t["net_pct"]
        if delay and t["exit_reason"] == "EARLY_TAKE_PROFIT":
            df = P.bars(t["symbol"])
            tgt = pd.Timestamp(t["exit_time"]) + pd.Timedelta(minutes=delay)
            w = df.loc[df["datetime"] <= tgt]
            if len(w):
                px = float(w["close"].iloc[-1])
                net = _net_return_pct(t["symbol"], t["entry_price"], px, 1)
        s["net_str"] = net - slip
        out.append(s)
    return K.size_chain(out, None, net_key="net_str")


print("=" * 84)
print("§14  체결 스트레스 (슬리피지는 전 거래, 지연은 ETP 청산에만)")
print("=" * 84)
print(f"  {'시나리오':22s} {'A 78d':>13s} {'F 78d':>13s} {'uplift':>11s}")
print("  " + "-" * 62)
for tag, slip, dly in (("기준", 0.0, 0), ("+0.05% 슬립", 0.05, 0), ("+0.10% 슬립", 0.10, 0),
                       ("+0.20% 슬립", 0.20, 0), ("ETP +1분 지연", 0.0, 1),
                       ("ETP +3분 지연", 0.0, 3), ("+3분 & 0.10% 슬립", 0.10, 3)):
    a, f = stressed(rA, slip, dly), stressed(rF, slip, dly)
    print(f"  {tag:22s} {K.krw_pnl(a, D):13,.0f} {K.krw_pnl(f, D):13,.0f} "
          f"{K.krw_pnl(f, D)-K.krw_pnl(a, D):+11,.0f}")

print()
print("=" * 84)
print("§11  LOO / L2O (변경 거래 단위)")
print("=" * 84)
def part(keep):
    out = [dict(fm[k]) if k in keep else dict(am[k]) for k in sorted(am)]
    return K.krw_pnl(K.size_chain(out, None), D) - A78
full = set(CHG)
loo = [part(full - {k}) for k in CHG]
print(f"  전체 {part(full):+,.0f} | LOO 양수 {sum(1 for x in loo if x>0)}/{len(loo)} "
      f"최소 {min(loo):+,.0f} 중앙 {st.median(loo):+,.0f}")
l2 = [part(full - set(c)) for c in itertools.combinations(CHG, 2)]
print(f"  L2O {len(l2)}조합 최소 {min(l2):+,.0f} 중앙 {st.median(l2):+,.0f} "
      f"음수 {sum(1 for x in l2 if x<=0)}/{len(l2)}")

print()
print("=" * 84)
print("§12  placebo 5,000회")
print("=" * 84)
real = part(full)
allk = sorted(am)
etpk = [k for k in allk if am[k]["exit_reason"] == "EARLY_TAKE_PROFIT"]
N = 5000
# (a) 임의 4거래에 'F 의 net' 을 부여 -> 대부분 변화 없음(ETP 아님) 이므로
#     ETP 가 아닌 거래에는 정책이 적용될 수 없다. 그래서 (b) 를 주 검정으로 쓴다.
pl = np.empty(N)
for i in range(N):
    pick = {allk[j] for j in rng.choice(len(allk), len(CHG), replace=False)}
    pl[i] = part(pick)
print(f"  (a) 임의 {len(CHG)}거래에 F 결과 부여: 실제 {real:+,.0f} | "
      f"p50 {np.percentile(pl,50):+,.0f} p95 {np.percentile(pl,95):+,.0f} | "
      f"percentile {(pl<real).mean()*100:.2f}%")
print(f"      -> ETP 청산이 아닌 거래는 F 와 net 이 같아 변화가 0 이다. "
      f"placebo 의 {int((pl==0).mean()*100)}% 가 정확히 0.")
# (b) ETP 6건 중 임의 절반(2건)만 완화
pl2 = np.empty(N)
for i in range(N):
    pick = {etpk[j] for j in rng.choice(len(etpk), 2, replace=False)}
    pl2[i] = part(pick)
half = [part(set(c)) for c in itertools.combinations(CHG, 2)]
print(f"  (b) ETP 6건 중 임의 2건만 완화: 실제(변경4건 전체) {real:+,.0f} | "
      f"p50 {np.percentile(pl2,50):+,.0f} p95 {np.percentile(pl2,95):+,.0f} | "
      f"percentile {(pl2<real).mean()*100:.2f}%")
print(f"      변경가능 4건 중 2건 조합의 실제 분포: 최소 {min(half):+,.0f} 최대 {max(half):+,.0f}")
