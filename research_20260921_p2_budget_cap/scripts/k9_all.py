# -*- coding: utf-8 -*-
"""§11 — 동일 DAILY_CAPITAL 3,000만원 기준 전 전략 재계산. READ-ONLY."""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
W30, W70 = D[-30:], D[-70:]
S = pickle.load(open("_strats.pkl", "rb"))


def to_rows(ts):
    out = [{
        "date": t["date"], "session": t["session"], "slot": t["slot_number"],
        "direction": t["direction"], "entry_time": t["entry_time"],
        "exit_time": t["exit_time"], "entry_price": t["entry_price"],
        "exit_price": t["exit_price"], "exit_reason": t["exit_reason"],
        "net_pct": t["net_pct"], "w1a_ref": t["w1a"], "peak_net_pct": t["peak_net_pct"],
        "mae_net_pct": t["mae_net_pct"], "entry_chop": t["entry_chop"],
        "tp1_hit": t["tp1_hit"], "hold_minutes": t["hold_minutes"],
        "symbol": "0193T0" if t["direction"] == "UP_RED" else "0197X0",
    } for t in ts if t.get("net_pct") is not None]
    out.sort(key=lambda t: (t["date"], t["entry_time"]))
    return out


TS_C1 = K.load()
ROWS = {
    "X2-lite W1":  to_rows(S["X2lite_W1"]),
    "H50":         to_rows(S["H50"]),
    "N1":          to_rows(S["N1"]),
    "N1-Safe":     to_rows(S["N1_Safe"]),
    "N1+C1":       TS_C1,
}

print("=" * 122)
print("§11  동일 DAILY_CAPITAL 30,000,000 KRW 기준 전 전략 재계산 (78일 20260527~20260918)")
print("=" * 122)
print(f"{'전략':16s} {'n':>4s} {'30d KRW':>12s} {'70d KRW':>13s} {'78d KRW':>13s} "
      f"{'PF':>6s} {'MDD%':>7s} {'-Top10':>13s} {'사용률':>7s} {'평균미사용':>12s} {'엔진복리%':>10s}")
print("-" * 122)
res = {}
for name, rows in ROWS.items():
    r = K.size_chain(rows, None)
    res[name] = r
    b = K.budget_stats(r, D)
    print(f"{name:16s} {len(r):4d} {K.krw_pnl(r, W30):12,.0f} {K.krw_pnl(r, W70):13,.0f} "
          f"{K.krw_pnl(r, D):13,.0f} {K.pf(r, D):6.3f} {K.mdd_krw(r, D):7.2f} "
          f"{K.excl_top_krw(r, D, 10):13,.0f} {b['util_pct']:6.1f}% {b['avg_unused']:12,.0f} "
          f"{K.compound_pct(r, D):9.2f}%")

# N1+C1+P2 (사용자정의) 와 연구원안
for tag, fn in (("N1+C1+P2", K.make_extra(1.05, 0.25, 1.00)),
                ("N1+C1+P2(연구원안)", lambda s, ss, dt=None: 0.25 if (s == 3 and ss == K.MORNING) else 1.05)):
    r = K.size_chain(TS_C1, fn)
    res[tag] = r
    b = K.budget_stats(r, D)
    print(f"{tag:16s} {len(r):4d} {K.krw_pnl(r, W30):12,.0f} {K.krw_pnl(r, W70):13,.0f} "
          f"{K.krw_pnl(r, D):13,.0f} {K.pf(r, D):6.3f} {K.mdd_krw(r, D):7.2f} "
          f"{K.excl_top_krw(r, D, 10):13,.0f} {b['util_pct']:6.1f}% {b['avg_unused']:12,.0f} "
          f"{K.compound_pct(r, D):9.2f}%")

print()
print(f"{'전략':16s} {'78d 단리%':>10s} {'78d 복리%':>10s} {'월환산%':>9s} {'최대일사용':>12s} {'cap발동':>7s}")
print("-" * 122)
for name, r in res.items():
    b = K.budget_stats(r, D)
    comp = K.krw_compound(r, D)
    print(f"{name:16s} {K.krw_pnl(r, D)/K.DAILY_CAPITAL*100:9.2f}% {comp:9.2f}% "
          f"{((1+comp/100)**(21/len(D))-1)*100:8.2f}% {b['max_used']:12,.0f} {b['cap_hits']:7d}")
