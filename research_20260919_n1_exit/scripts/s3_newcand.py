"""S3 — 종합 후보. 실행 전 확정(사후선택 없음).

X 계열: 지금까지 검증한 기전 중 **변경거래 교집합이 0** 인 것끼리 합성 (새 임계값 0)
Y 계열: 일손실 한도 — |stop_loss|=1.3 의 정수배만 사용 (신규진입만 금지, 청산 무개입)
Z 계열: 슬롯 사이징 — 기존 상수 X2LITE_SIZING_CHOP_MULT(0.80)/POST_STOP_MULT(1.20) 만
"""
import pickle, time, sys
sys.stdout.reconfigure(encoding="utf-8")
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
from common import summarize
from app.trading.macd2 import config

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
cfg, po, q3, h50 = A.SPEC["N1"]
base = A.run("N1", c, D); A.save()
mb = summarize(base, D); mb30 = summarize([t for t in base if t["date"] in S30], W30)
key = lambda t: (t["date"], t["entry_time"]); B = {key(t): t for t in base}
RUN8 = sorted(k for k, t in B.items() if t["peak_net_pct"] >= 8.0)
SL = abs(float(A.X.stop_loss_pct))   # 1.3
print(f"N1 78일 {mb['compound_pct']:.2f} / 30일 {mb30['compound_pct']:.2f}   |stop_loss|={SL}\n")

E35 = {"decide": 3.5, "strong": {}, "weak": {"close": True}}
GAPN = {"arm": 3.5, "give": 1.5, "cond": "gap_neg"}
BRK35W = {"arm": 3.5, "variant": "C3", "mode": "WEAK"}

CAND = {
  # X — 합성
  "X1_E35+gapneg":      dict(kw=dict(ax=dict(E35, pp=dict(GAPN)))),
  "X2_C3W+gapneg":      dict(kw=dict(ax={"decide": 3.0, "strong": {}, "weak": {},
                                         "brk": dict(BRK35W), "pp": dict(GAPN)})),
  "X3_E35+PD4020":      dict(kw=dict(ax=dict(E35, pp={"arm": 4.0, "give": 2.0}))),
  "X4_E35+C3W+gapneg":  dict(kw=dict(ax=dict(E35, brk=dict(BRK35W), pp=dict(GAPN)))),
  # Y — 일손실 한도
  "Y1_daystop-2.6":     dict(kw=dict(day_loss_stop=-2 * SL)),
  "Y2_daystop-3.9":     dict(kw=dict(day_loss_stop=-3 * SL)),
  "Y3_daystop-5.2":     dict(kw=dict(day_loss_stop=-4 * SL)),
  # Z — 슬롯 사이징
  "Z1_slot3x0.8":       dict(kw=dict(slot_mult={3: config.X2LITE_SIZING_CHOP_MULT})),
  "Z2_s3x0.8_s2x1.2":   dict(kw=dict(slot_mult={3: config.X2LITE_SIZING_CHOP_MULT,
                                                2: config.X2LITE_SIZING_POST_STOP_MULT})),
  "Z3_slot1x1.2":       dict(kw=dict(slot_mult={1: config.X2LITE_SIZING_POST_STOP_MULT})),
}

OUT = {"N1": base}
print(f"{'후보':22s} {'n':>4s} {'78d':>8s} {'Δ78':>8s} {'30d':>7s} {'Δ30':>7s} "
      f"{'PF':>6s} {'MDD':>7s} {'-T10':>7s} {'변경':>4s} {'run8':>6s}")
for k, sp in CAND.items():
    t0 = time.time()
    ts = A.run("N1", c, D, cfg=cfg, po=po, q3=q3, h50=h50, **sp["kw"])
    OUT[k] = ts; A.save()
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    ka = {key(t): t for t in ts}
    chg = sum(1 for x in set(ka) & set(B) if abs(ka[x]["net_pct"]-B[x]["net_pct"]) > 1e-9
              or abs(ka[x]["w1a"]-B[x]["w1a"]) > 1e-9)
    dmg = sum(ka[x]["net_pct"]-B[x]["net_pct"] for x in RUN8 if x in ka)
    print(f"{k:22s} {m['trades']:4d} {m['compound_pct']:8.2f} {m['compound_pct']-mb['compound_pct']:+8.2f} "
          f"{m30['compound_pct']:7.2f} {m30['compound_pct']-mb30['compound_pct']:+7.2f} "
          f"{m['pf']:6.3f} {m['mdd_pct']:7.2f} {m['top10_excl_pct']:7.2f} {chg:4d} {dmg:+6.2f}"
          f"  {time.time()-t0:.0f}s", flush=True)
pickle.dump({"trades": OUT, "dates": D}, open(A.HERE / "s3.pkl", "wb"))
print("\n저장: s3.pkl")
