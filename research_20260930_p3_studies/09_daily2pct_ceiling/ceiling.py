"""9월 '매일 최소 +2%' 가능성 검사: R0 / 필터 천장 / 완전예지 3회 천장. READ-ONLY."""
import pickle, sys
from pathlib import Path
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
RL = Path(sys.argv[1]); sys.path.insert(0, str(RL / "proj")); sys.path.insert(0, str(RL / "proj" / "scripts"))
from app.trading.trading_cost_engine import TradeCostEngine
T = pickle.load(open(RL / "out_H30_d83.pkl", "rb"))["trades"]
SEP = [d for d in pickle.load(open(RL / "_ctx83.pkl", "rb"))["dates"] if d.startswith("202609")]
pnl = lambda t: float(t["net_pct"]) * float(t["w1a"])
ce = TradeCostEngine()
fee = lambda sym: ce.compute_net_pnl(sym, 10000.0, 10000.0, 1000, buy_order_type="market", sell_order_type="market")["net_pnl"] / 1e7 * -100
def best_k(prices, k=3, cost=0.0):
    # 최대 k 회 비중복 매수→매도, 각 회 비용 cost(%), 로그수익 근사 대신 % 합
    import math
    n = len(prices); buy = [-math.inf] * (k + 1); sell = [0.0] * (k + 1)
    for pr in prices:
        for j in range(1, k + 1):
            buy[j] = max(buy[j], sell[j - 1] - pr)
            sell[j] = max(sell[j], buy[j] + pr - cost * pr / 100)
    return sell[k]
rows = []
for d in SEP:
    ts = [t for t in T if t["date"] == d]
    r0 = sum(pnl(t) for t in ts); fc = sum(max(0.0, float(t["net_pct"])) for t in ts)
    best = 0.0
    for tag, sym in (("long", "0193T0"), ("inverse", "0197X0")):
        x = pd.read_csv(RL / "cache83" / f"replay_{d}_{tag}_1m.csv", parse_dates=["datetime"]).set_index("datetime").between_time("09:00", "14:59")
        c = x["close"].astype(float).tolist(); base = c[0]
        best = max(best, best_k(c, 3, fee(sym)) / base * 100)
    # 두 ETF 혼합 3회는 개별 최대보다 클 수 있어 참고로 각 ETF 단독 3회 최대만 보고(하한 성격)
    rows.append((d, len(ts), r0, fc, best))
print("| 날짜 | R0 거래 | R0 하루 | 필터 천장(수익거래만) | 완전예지 3회 (ETF 단독) |")
print("|---|---|---|---|---|")
for d, n, r0, fc, b in rows:
    print(f"| {d[4:]} | {n} | {r0:+.2f} | {fc:+.2f} | {b:+.2f} |")
print(f"\nR0 ≥2% 인 날 {sum(r[2] >= 2 for r in rows)}/{len(rows)} · 필터 천장 ≥2% {sum(r[3] >= 2 for r in rows)}/{len(rows)} · 완전예지 ≥2% {sum(r[4] >= 2 for r in rows)}/{len(rows)}")
print("필터 천장 <2% 인 날:", [(r[0][4:], round(r[3], 2)) for r in rows if r[3] < 2])
