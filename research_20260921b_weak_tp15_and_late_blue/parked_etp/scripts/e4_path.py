# -*- coding: utf-8 -*-
"""§2 ETP 거래 전량 · §7 기여분해 · §9 발동 이후 경로. READ-ONLY."""
import sys, pickle
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
sys.path.insert(0, r"C:\Users\FURSYS\Desktop\AI-GAP 2")
import k1_core as K, t1_path as P, t2_regime as RG

D = pickle.load(open("_ctx_B.pkl", "rb"))["dates"]
TS = K.load(); A = K.size_chain(TS, None)
ETP = sorted((t for t in A if t["exit_reason"] == "EARLY_TAKE_PROFIT"), key=lambda x: x["date"])
RUN8 = {(t["date"], t["entry_time"]) for t in A if t["peak_net_pct"] >= 8.0}


def after(t):
    """ETP 청산 이후 **같은 날** 남은 1분봉 (장마감까지)."""
    df = P.bars(t["symbol"])
    x = pd.Timestamp(t["exit_time"])
    day_end = x.normalize() + pd.Timedelta(hours=15, minutes=30)
    m = (df["datetime"] > x) & (df["datetime"] <= day_end)
    return df.loc[m].reset_index(drop=True)


def before(t):
    df = P.bars(t["symbol"])
    e, x = pd.Timestamp(t["entry_time"]), pd.Timestamp(t["exit_time"])
    m = (df["datetime"] > e) & (df["datetime"] <= x)
    return df.loc[m].reset_index(drop=True)


print("=" * 128)
print("§2  ETP 거래 전량 (6건) — 발동 시점")
print("=" * 128)
print(f"{'날짜':9s} {'종목':7s} {'방향':10s} {'sl':>2s} {'세션':4s} {'진입':6s} {'진입가':>8s} "
      f"{'청산':6s} {'청산가':>8s} {'net%':>7s} {'KRW':>10s} {'chop':>5s} {'N1추세':>6s} "
      f"{'MFE':>6s} {'MAE':>6s} {'반납':>6s} {'보유분':>5s}")
print("-" * 128)
for t in ETP:
    print(f"{t['date']:9s} {t['symbol']:7s} {t['direction']:10s} {t['slot']:2d} "
          f"{('오전' if t['session']=='MORNING' else '오후'):4s} {t['entry_time'][11:16]:6s} "
          f"{t['entry_price']:8,.0f} {t['exit_time'][11:16]:6s} {t['exit_price']:8,.0f} "
          f"{t['net_pct']:7.3f} {t['pnl_krw']:10,.0f} {str(t['entry_chop']):>5s} "
          f"{str(RG.n1_trend_ok(t)):>6s} {t['peak_net_pct']:6.3f} {t['mae_net_pct']:6.3f} "
          f"{t['peak_net_pct']-t['net_pct']:6.3f} {t['hold_minutes']:5.0f}")

print()
print("=" * 128)
print("§2b  ETP 이후 같은 날 경로 (장마감까지) — '더 갔는가'")
print("=" * 128)
print(f"{'날짜':9s} {'청산':6s} {'net%':>7s} | {'이후MFE':>8s} {'이후MAE':>8s} | "
      f"{'+1.0':>5s} {'+1.25':>5s} {'+1.5':>5s} {'+1.75':>5s} {'+2.0':>5s} {'TP1(3.5)':>8s} "
      f"{'TP2(8.0)':>8s} {'손절(-1.3)':>9s} {'runner':>6s} {'남은봉':>6s}")
print("-" * 128)
rows = []
for t in ETP:
    af = after(t)
    if af.empty:
        print(f"{t['date']:9s} (이후 봉 없음)"); continue
    ns_h = [P.net_of(t["symbol"], t["entry_price"], float(h)) for h in af["high"]]
    ns_l = [P.net_of(t["symbol"], t["entry_price"], float(l)) for l in af["low"]]
    mfe, mae = max(ns_h), min(ns_l)
    hit = lambda v: "Y" if mfe >= v else "-"
    rows.append((t, mfe, mae))
    print(f"{t['date']:9s} {t['exit_time'][11:16]:6s} {t['net_pct']:7.3f} | {mfe:8.3f} {mae:8.3f} | "
          f"{hit(1.0):>5s} {hit(1.25):>5s} {hit(1.5):>5s} {hit(1.75):>5s} {hit(2.0):>5s} "
          f"{hit(3.5):>8s} {hit(8.0):>8s} {'Y' if mae <= -1.3 else '-':>9s} "
          f"{'Y' if (t['date'],t['entry_time']) in RUN8 else '-':>6s} {len(af):6d}")

print()
print("=" * 128)
print("§7  기여 분해 — ETP 가 도움이었나 방해였나 (이후 당일 경로 기준)")
print("=" * 128)
G = {"A 명백히 도움 (이후 손절선 도달)": [], "B 약간 도움 (이후 MFE < 실현net)": [],
     "C 명백히 방해 (이후 MFE >= 실현net+1.0%p)": [], "D 애매 (그 사이)": []}
for t, mfe, mae in rows:
    if mae <= -1.3:
        G["A 명백히 도움 (이후 손절선 도달)"].append((t, mfe, mae))
    elif mfe < t["net_pct"]:
        G["B 약간 도움 (이후 MFE < 실현net)"].append((t, mfe, mae))
    elif mfe >= t["net_pct"] + 1.0:
        G["C 명백히 방해 (이후 MFE >= 실현net+1.0%p)"].append((t, mfe, mae))
    else:
        G["D 애매 (그 사이)"].append((t, mfe, mae))
for g, v in G.items():
    if not v:
        print(f"  {g:38s} n=0"); continue
    print(f"  {g:38s} n={len(v)} ({len(v)/len(rows)*100:.0f}%)  "
          f"평균 이후MFE {sum(x[1] for x in v)/len(v):+.3f}  "
          f"평균 이후MAE {sum(x[2] for x in v)/len(v):+.3f}  "
          f"KRW {sum(x[0]['pnl_krw'] for x in v):+,.0f}")
    for t, mfe, mae in v:
        print(f"      {t['date']} {t['exit_time'][11:16]} net {t['net_pct']:+.2f} "
              f"-> 이후 MFE {mfe:+.2f} / MAE {mae:+.2f} (slot{t['slot']}, "
              f"{'오전' if t['session']=='MORNING' else '오후'})")

print()
print("=" * 128)
print("§9  ETP 발동 순간을 t=0 으로 정렬한 이후 경로 (net %, 종가 기준)")
print("=" * 128)
print(f"{'날짜':9s} {'t=0':>7s} " + " ".join(f"{f'+{m}분':>7s}" for m in (1,3,5,10,15,30)) + f" {'장마감':>7s}")
print("-" * 128)
import statistics as st
cols = {m: [] for m in (1,3,5,10,15,30)}
ends = []
for t in ETP:
    af = after(t)
    vals = []
    for m in (1,3,5,10,15,30):
        if len(af) >= m:
            v = P.net_of(t["symbol"], t["entry_price"], float(af["close"].iloc[m-1]))
            cols[m].append(v - t["net_pct"]); vals.append(v)
        else:
            vals.append(float("nan"))
    end = P.net_of(t["symbol"], t["entry_price"], float(af["close"].iloc[-1])) if len(af) else float("nan")
    ends.append(end - t["net_pct"])
    print(f"{t['date']:9s} {t['net_pct']:7.3f} " + " ".join(f"{v:7.3f}" for v in vals) + f" {end:7.3f}")
print("-" * 128)
print(f"{'평균 Δ':9s} {'':>7s} " + " ".join(f"{(sum(cols[m])/len(cols[m]) if cols[m] else 0):7.3f}" for m in (1,3,5,10,15,30))
      + f" {sum(ends)/len(ends):7.3f}")
print(f"{'중앙 Δ':9s} {'':>7s} " + " ".join(f"{(st.median(cols[m]) if cols[m] else 0):7.3f}" for m in (1,3,5,10,15,30))
      + f" {st.median(ends):7.3f}")
print("  (Δ = ETP 실현 net 대비. 양수면 '더 기다렸으면 좋았다')")
