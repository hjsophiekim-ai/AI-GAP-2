"""S4 — 조합 + 전체 배터리 + 위약. Z 계열은 '레버리지'라 위험정규화로 따로 본다."""
import pickle, time, sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize
from app.trading.macd2 import config
import axval as V

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
S3 = pickle.load(open(A.HERE / "s3.pkl", "rb"))
OUT = dict(S3["trades"]); base = OUT["N1"]
mb = summarize(base, D)
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
SL = abs(float(A.X.stop_loss_pct))
E35 = {"decide": 3.5, "strong": {}, "weak": {"close": True}}
GAPN = {"arm": 3.5, "give": 1.5, "cond": "gap_neg"}

NEW = {
  "W1_X1+Y2":      dict(ax=dict(E35, pp=dict(GAPN)), day_loss_stop=-3 * SL),
  "W2_X1+Y1":      dict(ax=dict(E35, pp=dict(GAPN)), day_loss_stop=-2 * SL),
  "W3_X1+Y2+Z2":   dict(ax=dict(E35, pp=dict(GAPN)), day_loss_stop=-3 * SL,
                        slot_mult={3: config.X2LITE_SIZING_CHOP_MULT,
                                   2: config.X2LITE_SIZING_POST_STOP_MULT}),
  # 위약 — X1 의 gap_neg 조건을 무조건 발동으로 (조건의 판정력 검증)
  "PL_X1_nocond":  dict(ax=dict(E35, pp={"arm": 3.5, "give": 1.5})),
}
for k, kw in NEW.items():
    if k in OUT:
        continue
    OUT[k] = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50, **kw); A.save()
# 기준 3전략
for k in ("H50", "N1_Safe", "X2lite_W1"):
    if k not in OUT:
        OUT[k] = A.run(k, c, D); A.save()
pickle.dump({"trades": OUT, "dates": D}, open(A.HERE / "s4.pkl", "wb"))

SHOW = ["N1_Safe", "H50", "X1_E35+gapneg", "X3_E35+PD4020", "Y2_daystop-3.9",
        "Y3_daystop-5.2", "Y1_daystop-2.6", "W1_X1+Y2", "W2_X1+Y1", "W3_X1+Y2+Z2",
        "Z2_s3x0.8_s2x1.2", "Z3_slot1x1.2", "Z1_slot3x0.8", "PL_X1_nocond",
        "X2_C3W+gapneg"]
print(f"{'후보':20s} {'n':>4s} {'Δ78':>8s} {'Δ30':>7s} {'PF':>6s} {'MDD':>6s} {'수익/MDD':>8s} "
      f"{'ΔT10':>7s} {'ΔT10_30':>8s} {'앞39':>7s} {'뒤39':>7s} {'WF':>6s} {'변경':>4s} "
      f"{'Σ단순':>7s} {'Σ-3':>7s} {'제외진입':>7s}")
ROWS = {}
for k in SHOW:
    ts = OUT[k]; r = V.report(k, ts, base, D); m = summarize(ts, D)
    ka = {key(t): t for t in ts}
    chg = sum(1 for x in set(ka) & set(B)
              if abs(ka[x]["net_pct"]-B[x]["net_pct"]) > 1e-9 or abs(ka[x]["w1a"]-B[x]["w1a"]) > 1e-9)
    ROWS[k] = (r, m)
    print(f"{k:20s} {m['trades']:4d} {r['78d_d']:+8.2f} {r['30d_d']:+7.2f} {m['pf']:6.3f} "
          f"{m['mdd_pct']:6.2f} {m['compound_pct']/abs(m['mdd_pct']):8.2f} "
          f"{r['top10_d']:+7.2f} {r['top10_d30']:+8.2f} {r['half'][0]:+7.2f} {r['half'][1]:+7.2f} "
          f"{r['wf6_win']:2d}/{r['wf6_lose']:<2d} {chg:4d} {r['sum_d']:+7.2f} {r['sum_d_ex3']:+7.2f} "
          f"{r['only_b']:7d}")
print(f"{'N1(기준)':20s} {mb['trades']:4d} {0.0:+8.2f} {0.0:+7.2f} {mb['pf']:6.3f} "
      f"{mb['mdd_pct']:6.2f} {mb['compound_pct']/abs(mb['mdd_pct']):8.2f}")

print("\n" + "="*120)
print("채택조건 상세 — Δ78·Δ30 모두 비악화 후보")
print("="*120)
RUN8 = sorted(x for x, t in B.items() if t["peak_net_pct"] >= 8.0)
for k in SHOW:
    r, m = ROWS[k]
    if not (r["78d_d"] >= -1e-9 and r["30d_d"] >= -1e-9):
        continue
    ka = {key(t): t for t in OUT[k]}
    dmg = sum(ka[x]["net_pct"]-B[x]["net_pct"] for x in RUN8 if x in ka)
    cond = {
        "30d 비악화": r["30d_d"] >= -1e-9, "78d 비악화": r["78d_d"] >= -1e-9,
        "PF 비악화(78)": (r["78d_pf_a"] or 0) >= (r["78d_pf_b"] or 0)-1e-9,
        "PF 비악화(30)": (r["30d_pf_a"] or 0) >= (r["30d_pf_b"] or 0)-1e-9,
        "MDD 비악화(78)": r["78d_mdd_a"] >= r["78d_mdd_b"]-1e-9,
        "MDD 비악화(30)": r["30d_mdd_a"] >= r["30d_mdd_b"]-1e-9,
        "-Top10 비악화(78)": r["top10_d"] >= -1e-9,
        "-Top10 비악화(30)": r["top10_d30"] >= -1e-9,
        "앞39/뒤39 둘다 비악화": all(x >= -1e-9 for x in r["half"]),
        "WF 4/6 이상": r["wf6_win"] >= 4,
        "상위3 개선 제외 uplift>=0": r["sum_d_ex3"] >= 0,
        "runner 손상 없음": dmg >= -1e-9,
    }
    print(f"\n### {k}   78일 {r['78d_a']:.2f} ({r['78d_d']:+.2f})  30일 {r['30d_a']:.2f} ({r['30d_d']:+.2f})")
    print(f"  PF {r['78d_pf_b']:.3f}->{r['78d_pf_a']:.3f} / {r['30d_pf_b']:.3f}->{r['30d_pf_a']:.3f}"
          f"   MDD {r['78d_mdd_b']:.2f}->{r['78d_mdd_a']:.2f} / {r['30d_mdd_b']:.2f}->{r['30d_mdd_a']:.2f}")
    print(f"  -Top1/3/10(78) {r['top1_d']:+.2f}/{r['top3_d']:+.2f}/{r['top10_d']:+.2f}"
          f"  (30) {r['top1_d30']:+.2f}/{r['top3_d30']:+.2f}/{r['top10_d30']:+.2f}")
    print(f"  앞39/뒤39 {r['half'][0]:+.2f}/{r['half'][1]:+.2f}   5분할 " +
          " ".join(f"{x:+.2f}" for x in r["k5"]))
    print(f"  WF6 " + " ".join(f"{x:+.2f}" for x in r["wf6"]) +
          f"  → {r['wf6_win']}승 {r['wf6_lose']}패 {6-r['wf6_win']-r['wf6_lose']}무")
    print(f"  변경 {r['chg']} (개선 {r['up']}/악화 {r['dn']})  단순합 {r['sum_d']:+.2f}  "
          f"상위1/2/3 제외 {r['sum_d_ex1']:+.2f}/{r['sum_d_ex2']:+.2f}/{r['sum_d_ex3']:+.2f}  "
          f"runner {dmg:+.2f}  진입제외 {r['only_b']}건")
    print("  " + " | ".join(f"{'PASS' if v else 'FAIL'} {kk}" for kk, v in cond.items()))
    print(f"  => {'채택' if all(cond.values()) else '기각'} 미충족={[kk for kk,v in cond.items() if not v]}")
