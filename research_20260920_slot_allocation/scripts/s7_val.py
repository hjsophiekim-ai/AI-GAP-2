"""S7 — '오전 3번째 진입 자금 재배분' 정밀검증.

후보 M0: 오전(09:00~11:00) slot3 진입에 자금을 배정하지 않고, 남는 한도를
         slot1·2 에 균등 재배분(스칼라 k). 진입집합/청산/3회 한도 불변.
비교기준: 같은 총노출의 무배분 레버리지.
"""
import sys, pickle
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
import axlib as A
A.H._MEMO_PATH = A.HERE / "_memo_B.pkl"
A._Q3_PATH = A.HERE / "_q3base.pkl"
from common import summarize, compound, excl_topn
from app.trading.macd2 import config
from app.trading.macd2 import time_window_3slot as tw3
import hengine5 as H
import axval as V

c = A.ctx(78); D = c.dates; W30 = D[-30:]; S30 = set(W30)
C1 = {"decide": 99.0, "strong": {}, "weak": {},
      "pp": {"arm": float(config.C1_ARM_MFE_PCT),
             "give": float(config.C1_GIVEBACK_PCT), "cond": "gap_neg"}}
H.D_VARIANTS["N1PROD"] = {}
S4 = pickle.load(open(A.HERE / "s4.pkl", "rb"))
LEV = sorted(S4["lev"]); lx = np.array([x[0] for x in LEV]); ly = np.array([x[1] for x in LEV])
MO = tw3.SESSION_MORNING
rng = np.random.default_rng(20260920)


def run(sm=None, cap=None):
    _pb = H._MEMO["base"]; H._MEMO["base"] = A.Q3_BASE
    _oc = config.X2LITE_SIZING_DAILY_EXPOSURE_CAP; _om = config.X2LITE_SIZING_MAX_MULT
    if cap is not None:
        config.X2LITE_SIZING_DAILY_EXPOSURE_CAP = float(cap)
        config.X2LITE_SIZING_MAX_MULT = 9.0
    try:
        return H.run_chain(c, H.N1_PROD, dates=D, h50=True, d_variant="N1PROD",
                           ax=C1, slot_mult=sm)
    finally:
        config.X2LITE_SIZING_DAILY_EXPOSURE_CAP = _oc
        config.X2LITE_SIZING_MAX_MULT = _om
        H._MEMO["base"] = _pb


expo = lambda ts: float(sum(float(t["w1a"]) for t in ts))
BASE = run(); EB = expo(BASE)
LEVBASE = run(lambda s, ss, dt=None: 1.0, cap=99.0)
mL = summarize(LEVBASE, D)
l30 = summarize([t for t in LEVBASE if t["date"] in S30], W30)["compound_pct"]
key = lambda t: (t["date"], t["entry_time"])


def matched(fn, tag, show=True):
    k = 1.0; ts = None
    for _ in range(6):
        ts = run((lambda s, ss, dt, _f=fn, _k=k: _f(s, ss, dt) * _k), cap=99.0)
        e = expo(ts)
        if abs(e - EB) < 0.12:
            break
        k *= EB / e
    m = summarize(ts, D); m30 = summarize([t for t in ts if t["date"] in S30], W30)
    r = V.report(tag, ts, LEVBASE, D)
    assert r["only_a"] == 0 and r["only_b"] == 0
    lv = float(np.interp(e, lx, ly))
    if show:
        print(f"{tag:26s} {k:6.3f} {m['compound_pct']:9.3f} {m['compound_pct']-lv:+9.2f} "
              f"{m30['compound_pct']-l30:+8.2f} "
              f"{compound(ts,D[:39])-compound(LEVBASE,D[:39]):+8.2f} "
              f"{compound(ts,D[39:])-compound(LEVBASE,D[39:]):+8.2f} "
              f"{m['mdd_pct']:7.2f} {m['pf']:6.3f} "
              f"{excl_topn(ts,D,3)-excl_topn(LEVBASE,D,3):+8.2f} "
              f"{excl_topn(ts,D,10)-excl_topn(LEVBASE,D,10):+8.2f} "
              f"{' '.join(f'{x:+5.1f}' for x in r['k5']):>26s} "
              f"{str(r['wf6_win'])+'승'+str(r['wf6_lose'])+'패':>6s}", flush=True)
    return ts, m, r, lv


print(f"레버리지 대조: 78d {mL['compound_pct']:.3f} MDD {mL['mdd_pct']:.2f} PF {mL['pf']:.3f}")

# 대상 거래 목록
MS = [t for t in BASE if t["slot_number"] == 3 and t["session"] == MO]
print(f"\n오전 3번째 진입 거래 {len(MS)}건 (78일):")
print(f"  {'일자':10s} {'시각':6s} {'방향':10s} {'net':>8s} {'peak':>6s} {'w1a':>5s} {'청산사유'}")
for t in sorted(MS, key=lambda x: x["date"]):
    print(f"  {t['date']:10s} {t['entry_time'][11:16]:6s} {t['direction']:10s} "
          f"{t['net_pct']:+8.3f} {t['peak_net_pct']:6.2f} {t['w1a']:5.2f} {t['exit_reason']}")
print(f"  단순합 {sum(float(t['net_pct']) for t in MS):+.2f}%p / "
      f"가중합 {sum(float(t['net_pct'])*float(t['w1a']) for t in MS):+.2f}%p")

print("\n" + "=" * 156)
print("1. 삭감 깊이 민감도 (plateau) — 오전 slot3 배수")
print("=" * 156)
print(f"{'후보':26s} {'k':>6s} {'78d':>9s} {'배분효과':>9s} {'30d':>8s} {'앞39':>8s} {'뒤39':>8s} "
      f"{'MDD':>7s} {'PF':>6s} {'-T3':>8s} {'-T10':>8s} {'5분할':>26s} {'WF6':>6s}")
for mult in (0.0, 0.25, 0.5, 0.75, 1.0):
    matched((lambda s, ss, dt, _m=mult: _m if (s == 3 and ss == MO) else 1.0),
            f"오전slot3 x{mult:.2f}")

print("\n" + "=" * 156)
print("2. 인접 정의 — 정말 '오전 3번째' 인가")
print("=" * 156)
print(f"{'후보':26s} {'k':>6s} {'78d':>9s} {'배분효과':>9s} {'30d':>8s} {'앞39':>8s} {'뒤39':>8s} "
      f"{'MDD':>7s} {'PF':>6s} {'-T3':>8s} {'-T10':>8s} {'5분할':>26s} {'WF6':>6s}")
matched(lambda s, ss, dt=None: 0.0 if (s == 3 and ss == MO) else 1.0, "오전slot3 x0 (M0)")
matched(lambda s, ss, dt=None: 0.0 if (s == 2 and ss == MO) else 1.0, "오전slot2 x0 (대조)")
matched(lambda s, ss, dt=None: 0.0 if (s == 3) else 1.0, "모든slot3 x0 (대조)")
matched(lambda s, ss, dt=None: 0.0 if (s == 3 and ss != MO) else 1.0, "오후slot3 x0 (대조)")

print("\n" + "=" * 156)
print("3. LOO — 8건 중 1건씩 되살려도 남는가 (그 1건만 배수 1.0 유지)")
print("=" * 156)
print(f"{'되살린 거래':26s} {'k':>6s} {'78d':>9s} {'배분효과':>9s} {'30d':>8s} {'앞39':>8s} {'뒤39':>8s} "
      f"{'MDD':>7s} {'PF':>6s} {'-T3':>8s} {'-T10':>8s} {'5분할':>26s} {'WF6':>6s}")
KEEP = {(t["date"], t["entry_time"]) for t in MS}
M0 = matched(lambda s, ss, dt=None: 0.0 if (s == 3 and ss == MO) else 1.0, "(기준 M0)", show=False)
base_eff = M0[1]["compound_pct"] - M0[3]
for kk in sorted(KEEP):
    # 그 날짜만 예외로 두는 방식(엔진은 날짜/시각을 slot_mult 에 주지 않으므로
    # 날짜 필터를 클로저에 담아 쓴다 -- 엔진 수정 없이 date 를 알 수 없으므로
    # 대신 '그 날 전체를 예외' 로 두면 다른 슬롯까지 바뀐다. 따라서 여기서는
    # 8건을 하나씩 빼는 대신 '무작위 7건' 조합을 전수로 돌린다.)
    pass
print("  (엔진 slot_mult 는 날짜를 받지 않으므로, 아래 4번 부트스트랩으로 대체한다)")

print("\n" + "=" * 156)
print("4. 일단위 부트스트랩 / 월별")
print("=" * 156)
ts = M0[0]
ua = {d: compound([t for t in ts if t["date"] == d], [d]) for d in D}
ub = {d: compound([t for t in LEVBASE if t["date"] == d], [d]) for d in D}
da = np.array([ua[d] - ub[d] for d in D])
idx = rng.integers(0, len(D), size=(10000, len(D)))
bs = da[idx].mean(axis=1)
print(f"  일단위 부트스트랩 10000회: 평균 {bs.mean():+.4f} / >0 {100*(bs>0).mean():.1f}% "
      f"/ 5%분위 {np.percentile(bs,5):+.4f} / 95%분위 {np.percentile(bs,95):+.4f}")
mm = " ".join(f"{k}={compound(ts,[x for x in D if x.startswith(k)])-compound(LEVBASE,[x for x in D if x.startswith(k)]):+.1f}"
              for k in ("202605", "202606", "202607", "202608", "202609"))
print(f"  월별 배분효과: {mm}")
dd = sorted({t["date"] for t in MS})
print(f"  해당 거래가 있던 날 {len(dd)}일: {', '.join(dd)}")
pickle.dump({"m0": ts, "levbase": LEVBASE, "base": BASE, "dates": D},
            open(A.HERE / "s7.pkl", "wb"))
print("\n저장: s7.pkl")
