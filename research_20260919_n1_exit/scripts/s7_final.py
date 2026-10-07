"""S7 — 최종 검증.
 (A) 다른 기저전략(X2-lite W1 / H50 / N1-Safe)에 같은 규칙을 이식 — 외적타당성
 (B) 위약 — gap 부호를 실측 비율의 난수로 대체
 (C) 창별 표 + 변경거래 전량
"""
import pickle, sys, statistics as st
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
import hengine5 as H
from app.trading.macd2.models import Direction
from common import summarize, compound, excl_topn
import axval as V

c = A.ctx(78); D = c.dates
PPR = {"arm": 5.0, "give": 1.5, "cond": "gap_neg"}
NC = {"decide": 99.0, "strong": {}, "weak": {}}
OUT = {}
for k in ("N1", "N1_Safe", "H50", "X2lite_W1"):
    OUT[k] = A.run(k, c, D); A.save()

print("=== (A) 같은 규칙을 다른 기저전략에 이식 ===", flush=True)
print(f"{'기저':12s} {'기저 78d':>9s} {'+PP 78d':>9s} {'Δ78':>8s} {'기저 30d':>8s} {'+PP 30d':>8s} "
      f"{'Δ30':>7s} {'ΔPF':>7s} {'ΔMDD':>6s} {'ΔT10':>7s} {'변경':>4s} {'↑/↓':>6s}")
for k in ("X2lite_W1", "H50", "N1_Safe", "N1"):
    cfg, po, q3, h50 = A.SPEC[k]
    kk = f"{k}+PP5.0"
    OUT[kk] = A.run(k, c, D, cfg=cfg, po=po, q3=q3, h50=h50, ax=dict(NC, pp=dict(PPR)))
    A.save()
    r = V.report(kk, OUT[kk], OUT[k], D)
    mb = summarize(OUT[k], D); ma = summarize(OUT[kk], D)
    mb30 = summarize([t for t in OUT[k] if t["date"] in set(D[-30:])], D[-30:])
    ma30 = summarize([t for t in OUT[kk] if t["date"] in set(D[-30:])], D[-30:])
    print(f"{k:12s} {mb['compound_pct']:9.2f} {ma['compound_pct']:9.2f} {r['78d_d']:+8.2f} "
          f"{mb30['compound_pct']:8.2f} {ma30['compound_pct']:8.2f} {r['30d_d']:+7.2f} "
          f"{(ma['pf'] or 0)-(mb['pf'] or 0):+7.3f} {ma['mdd_pct']-mb['mdd_pct']:+6.2f} "
          f"{r['top10_d']:+7.2f} {r['chg']:4d} {r['up']:3d}/{r['dn']:<3d}", flush=True)

print("\n=== (B) 위약 — gap 부호를 실측비율 난수로 ===", flush=True)
rate = (H.gapneg_rate(c.hynix_bars_3m, Direction.UP_RED)
        + H.gapneg_rate(c.hynix_bars_3m, Direction.DOWN_BLUE)) / 2
print(f"  실측 gap<=0 완성봉 비율 p={rate:.4f}", flush=True)
cfg, po, q3, h50 = A.SPEC["N1"]
real = V.report("real", OUT["N1+PP5.0"], OUT["N1"], D)
print(f"  실제: Δ78={real['78d_d']:+.2f} Δ30={real['30d_d']:+.2f} "
      f"앞={real['half'][0]:+.2f} 뒤={real['half'][1]:+.2f} WF={real['wf6_win']}/{real['wf6_lose']} "
      f"변경={real['chg']}({real['up']}↑{real['dn']}↓)", flush=True)
ds = []
for seed in range(12):
    H._PL[0] = seed; H._PL[1] = rate
    ts = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50,
               ax=dict(NC, pp={"arm": 5.0, "give": 1.5, "cond": "placebo"}))
    A.save(); OUT[f"PL{seed}"] = ts
    r = V.report(f"PL{seed}", ts, OUT["N1"], D)
    ds.append((r["78d_d"], r["30d_d"]))
    print(f"    위약 seed={seed:2d} Δ78={r['78d_d']:+8.2f} Δ30={r['30d_d']:+6.2f} "
          f"변경={r['chg']:3d}({r['up']}↑{r['dn']}↓) 앞={r['half'][0]:+7.2f} 뒤={r['half'][1]:+6.2f}",
          flush=True)
for j, tag in ((0, "78d"), (1, "30d")):
    xs = [x[j] for x in ds]; rv = real[f"{tag}_d"]
    print(f"  >> {tag}: 실제={rv:+.2f} 위약 평균={st.mean(xs):+.2f} 최대={max(xs):+.2f} "
          f"최소={min(xs):+.2f} 위약>=실제 {sum(1 for x in xs if x >= rv)}/{len(xs)}", flush=True)
pickle.dump({"trades": OUT, "dates": D}, open(A.HERE / "s7.pkl", "wb"))
print("저장: s7.pkl")
