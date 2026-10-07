"""G3 — CHOP RESPONSE 배터리. 전부 실제 엔진 replay. 재개 가능.

regime 은 SHADOW-BASE 사전계산 배열(shadow_on.npy) — feedback loop 없음.
CHOP OFF 구간에서는 어떤 규칙도 적용되지 않으므로 BASE 와 diff 0 이어야 한다
(G0_BASE parity 로 확인).
"""
import pickle, sys, time
from pathlib import Path
import numpy as np, pandas as pd
sys.path.insert(0, str(Path(__file__).resolve().parent))
import rlib as R
import axlib as A
import hengine5 as H
from common import summarize
from app.trading.macd2 import teg_gate as TG
from app.trading.macd2.models import Direction

HERE = Path(__file__).resolve().parent
OUT = HERE / "g3.pkl"
ctx = R.get_ctx()
D = list(ctx.dates)
bars = ctx.hynix_bars_3m.reset_index(drop=True)
SH = np.load(HERE / "shadow_on.npy")

# ── 강도 점수 (기존 feature 만 사용) — 플래그 확정봉에서만 계산 ──────────
close = bars["close"].astype(float).values
high = bars["high"].astype(float).values
low = bars["low"].astype(float).values
vol = bars["volume"].astype(float).values
date = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d").values
ef = bars["close"].ewm(span=12, adjust=False).mean()
es = bars["close"].ewm(span=26, adjust=False).mean()
gap = (ef - es - (ef - es).ewm(span=9, adjust=False).mean()).values
e20 = bars["close"].ewm(span=20, adjust=False).mean().values
e50 = bars["close"].ewm(span=50, adjust=False).mean().values
vwap = np.full(len(bars), np.nan)
cur, pv, pvv = None, 0.0, 0.0
tp3 = (high + low + close) / 3.0
for i in range(len(bars)):
    if date[i] != cur:
        cur, pv, pvv = date[i], 0.0, 0.0
    v = vol[i] if vol[i] > 0 else 1.0
    pv += tp3[i] * v; pvv += v
    vwap[i] = pv / pvv
flags = ctx.flags_by_idx
fidx = sorted(flags)

score = np.full(len(bars), np.nan)
teg_ok = np.zeros(len(bars), bool)
for k in fidx:
    i = k + 1                       # 진입 판정봉
    if i >= len(bars):
        continue
    d = flags[k]
    s = 1.0 if d == Direction.UP_RED else -1.0
    try:
        t = TG.evaluate_teg(bars.iloc[: i + 1], d,
                            pd.Timestamp(bars["datetime"].iloc[k]).to_pydatetime(),
                            pd.Timestamp(bars["datetime"].iloc[i]).to_pydatetime()
                            + pd.Timedelta(minutes=3))
        cond = dict(getattr(t, "conditions", None) or {})
        full = bool(getattr(t, "approved", False))
    except Exception:
        cond, full = {}, False
    teg_ok[i] = full
    sc = 0
    sc += 1 if full else 0
    if i >= 2 and date[i - 2] == date[i]:
        sc += 1 if s * (gap[i] - gap[i - 2]) > 0 else 0
    if i >= 1 and date[i - 1] == date[i]:
        sc += 1 if s * ((e20[i] - e50[i]) - (e20[i - 1] - e50[i - 1])) > 0 else 0
    sc += 1 if s * (close[i] - vwap[i]) > 0 else 0
    prev = [x for x in fidx if x < k and date[x] == date[i]]
    sc += 1 if (not prev or (k - prev[-1]) >= 3) else 0
    rec = [x for x in fidx if i - 20 <= x <= i and date[x] == date[i]]
    sc += 1 if len(rec) <= 2 else 0
    score[i] = sc

vals = score[~np.isnan(score)]
TH50 = float(np.quantile(vals, 0.50))
TH33 = float(np.quantile(vals, 0.67))
print("강도점수 분포 n=%d  중앙 %.1f  상위50%% 경계 %.1f  상위33%% 경계 %.1f  TEG full %d"
      % (len(vals), np.median(vals), TH50, TH33, int(teg_ok.sum())), flush=True)

FEAT = {"score": score, "teg_ok": teg_ok}


def is_ar1(day, rec_at):
    kst = rec_at.astimezone(H.KST)
    return (kst.strftime("%Y%m%d"), kst.strftime("%H:%M")) in R.Z.RELAXED_KEYS


def gx(**kw):
    g = {"on": SH, "feat": FEAT, "is_ar1": is_ar1, "log": []}
    g.update(kw)
    return g


BAT = [
    ("G0_BASE", None),
    # 축 B — 짧게 먹고 빠르게 청산
    ("B1_tp06_sl08_m15", gx(exit={"mode": "replace", "tp": 0.6, "sl": 0.8, "maxmin": 15})),
    ("B2_tp08_sl08_m15", gx(exit={"mode": "replace", "tp": 0.8, "sl": 0.8, "maxmin": 15})),
    ("B3_tp10_sl10_m20", gx(exit={"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20})),
    ("B4_be08_m20", gx(exit={"mode": "overlay", "be_after": 0.8, "maxmin": 20})),
    ("B5_part10", gx(exit={"mode": "overlay", "partial": {"at": 1.0, "ratio": 0.5}})),
    # 축 A/C — 강도 점수 기반 skip (축 A 는 feature ranking 이 전무해 C 와 동일 기계로 수행)
    ("C1_top50", gx(skip={"rule": "C1", "params": {"th": TH50}})),
    ("C2_top33", gx(skip={"rule": "C2", "params": {"th": TH33}})),
    ("C3_tegfull", gx(skip={"rule": "C3", "params": {}})),
    # 축 D — 슬롯/재진입 통제
    ("D1_slot1only", gx(skip={"rule": "D1", "params": {}})),
    ("D2_firstloss", gx(skip={"rule": "D2", "params": {}})),
    ("D3_max2", gx(skip={"rule": "D3", "params": {"max": 2}})),
    ("D4_streak2", gx(skip={"rule": "D4", "params": {"n": 2}})),
    # 시간대 대조군
    ("T1_1000_1130", gx(skip={"rule": "T1", "params": {}})),
]

state = pickle.load(open(OUT, "rb")) if OUT.exists() else {"runs": {}, "logs": {}}
state["dates"] = D
state["bat"] = [b[0] for b in BAT]
state["th"] = {"TH50": TH50, "TH33": TH33}
t00 = time.time()
for tag, g in BAT:
    if tag in state["runs"]:
        print("%-20s (건너뜀)" % tag, flush=True)
        continue
    R.Z.set_gate(lambda f: True)
    R.Z.RELAXED_KEYS.clear()
    R.MODE["on"] = True
    t0 = time.time()
    ts = A.run("N1", ctx, D, ax=R.AX, **({"gx": g} if g else {}))
    m = summarize(ts, D)
    nch = sum(1 for t in ts if t.get("gx_chop"))
    print("%-20s 거래 %3d (CHOP진입 %2d)  복리 %9.4f  PF %.3f  MDD %7.3f  승률 %5.1f%%  (%.0fs)"
          % (tag, m["trades"], nch, m["compound_pct"], m["pf"], m["mdd_pct"],
             m["win_rate_pct"], time.time() - t0), flush=True)
    state["runs"][tag] = ts
    state["logs"][tag] = (g or {}).get("log", [])
    pickle.dump(state, open(OUT, "wb"))
print("\n총 %.0f분 · saved g3.pkl" % ((time.time() - t00) / 60))
