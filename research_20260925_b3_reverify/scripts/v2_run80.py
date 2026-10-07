"""V2 - 정확한 80영업일(20260527~20260922) BASE 2회 + B3. 앵커 재검증 포함."""
import sys, time, pickle
from pathlib import Path
from dataclasses import replace
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
# axlib/rlib 가 import 시점에 ce.CACHE_DIR / H._CTX_CACHE / H._MEMO_PATH 를 자기 값으로
# 덮어쓰므로, 경로 지정은 반드시 **그 import 뒤**에 해야 한다.
import axlib as A
import rlib as R
from app.trading.macd2 import config
from common import summarize

ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx81.pkl"
H._MEMO_PATH = HERE / "_memo81.pkl"

t0 = time.time()
ctx = H.build_ctx(81)
ALL = list(ctx.dates)
D80 = [d for d in ALL if "20260527" <= d <= "20260922"]
print("ctx %d일 %s~%s (%.0fs)" % (len(ALL), ALL[0], ALL[-1], time.time() - t0), flush=True)
print("대상 80일: %s ~ %s (%d일)" % (D80[0], D80[-1], len(D80)), flush=True)
assert len(D80) == 80, "80일이 아니다: %d" % len(D80)

# ── 앵커 (ctx 재빌드 후 필수) ──
H.load_memo(); PROD = H._MEMO["base"]; Q3 = {}
pres = pickle.load(open(HERE / "_ctx76_preserved.pkl", "rb"))
W70 = pres["dates"][-70:]
X = H.X2LITE
NB = {"tp1": 3.5, "tp2": 8.0, "tp1_ratio": 0.0, "off_tp2": 4.0}
V = {"X2lite_W1": (None, {}, False, False), "H50": (None, {}, False, True),
     "N1": (dict(NB), {"trail_stop_pct": 1.5, "aft_tp_pct": 4.0}, True, True)}
EXP = {"X2lite_W1": (144, 176.3247), "H50": (141, 204.1759), "N1": (142, 396.6787)}
ok = True
for k, (cfg, po, q3, h50) in V.items():
    if cfg is not None:
        H.D_VARIANTS[k] = cfg
    p_ = replace(X, **po) if po else X
    kw = dict(h50=h50)
    if cfg is not None:
        kw["d_variant"] = k
    if q3:
        H._MEMO["base"] = Q3
        old = config.QUALITY_SCORE_THRESHOLD; config.QUALITY_SCORE_THRESHOLD = 3
        try:
            tr = H.run_chain(ctx, p_, dates=W70, **kw)
        finally:
            config.QUALITY_SCORE_THRESHOLD = old; H._MEMO["base"] = PROD
    else:
        tr = H.run_chain(ctx, p_, dates=W70, **kw)
    m = summarize(tr, W70); n_e, c_e = EXP[k]
    g = m["trades"] == n_e and abs(m["compound_pct"] - c_e) < 0.01
    ok &= g
    print("앵커 %-11s n=%4d 복리=%10.4f (기대 %9.4f) %s" % (k, m["trades"], m["compound_pct"], c_e,
                                                        "OK" if g else "<<< 불일치"), flush=True)
H.save_memo()
assert ok, "앵커 불일치 — 중단"
print("앵커 OK\n", flush=True)

A.PROD_BASE = H._MEMO["base"]
R.Z.FEAT = R.Z.build_features(ctx.hynix_bars_3m)
R.Z.install()


def run(tag, gx=None):
    for kk in list(H._MEMO):
        H._MEMO[kk] = {}
    A.PROD_BASE = H._MEMO["base"]; A.Q3_BASE = {}
    R.Z.set_gate(lambda f: True); R.Z.RELAXED_KEYS.clear(); R.MODE["on"] = True
    t = time.time()
    ts = A.run("N1", ctx, D80, ax=R.AX, **({"gx": gx} if gx else {}))
    m = summarize(ts, D80)
    print("%-10s 거래 %3d 복리 %9.4f PF %.3f MDD %7.3f 승률 %5.1f%% (%.0fs)"
          % (tag, m["trades"], m["compound_pct"], m["pf"], m["mdd_pct"],
             m["win_rate_pct"], time.time() - t), flush=True)
    return ts


b1 = run("BASE")
b2 = run("BASE2")


def sig(ts):
    return sorted((t["date"], str(t["entry_time"]), t["direction"], str(t["exit_time"]),
                   round(float(t["net_pct"]), 8)) for t in ts)


same = sig(b1) == sig(b2)
print("BASE 2회 동일성:", "완전 일치" if same else "불일치!!", flush=True)
assert same

# ── SHADOW detector (80일 BASE 기준) ──
B = pd.DataFrame(b1).sort_values("exit_time").reset_index(drop=True)
ext = pd.to_datetime(B.exit_time, utc=True).values
h50v = B.h50_held.astype(float).values
tp1v = B.tp1_hit.astype(float).values
bars = ctx.hynix_bars_3m.reset_index(drop=True)
rec = (pd.to_datetime(bars["datetime"]).dt.tz_localize(None)
       if pd.to_datetime(bars["datetime"]).dt.tz is None
       else pd.to_datetime(bars["datetime"]).dt.tz_convert("UTC").dt.tz_localize(None))
rec = rec + pd.Timedelta(minutes=3)
extn = pd.to_datetime(B.exit_time, utc=True).dt.tz_localize(None).values
SH = np.zeros(len(bars), bool)
for i in range(len(bars)):
    n = int(np.searchsorted(extn, np.datetime64(rec.iloc[i]), side="left"))
    if n >= 10:
        SH[i] = (h50v[n - 10:n].mean() >= 0.40) and (tp1v[n - 10:n].mean() <= 0.20)
np.save(HERE / "shadow_on80.npy", SH)
bd = pd.to_datetime(bars["datetime"]).dt.strftime("%Y%m%d").values
sep = [d for d in D80 if d >= "20260901"]
t2 = pd.DataFrame({"d": bd, "on": SH}).groupby("d").on.mean()
print("SHADOW ON 봉 %d/%d · 9월 ON %.1f%% · 비9월 ON %.1f%%"
      % (SH.sum(), len(SH), 100 * t2[[d for d in t2.index if d in set(sep)]].mean(),
         100 * t2[[d for d in t2.index if d in set(D80) and d < "20260901"]].mean()), flush=True)


def is_ar1(day, rec_at):
    kk = rec_at.astimezone(H.KST)
    return (kk.strftime("%Y%m%d"), kk.strftime("%H:%M")) in R.Z.RELAXED_KEYS


b3 = run("B3", gx={"on": SH, "is_ar1": is_ar1, "log": [],
                   "exit": {"mode": "replace", "tp": 1.0, "sl": 1.0, "maxmin": 20}})
mark = run("MARK", gx={"on": SH, "is_ar1": is_ar1, "log": []})
print("MARK vs BASE 동일:", sig(mark) == sig(b1), flush=True)
pickle.dump({"dates": D80, "base": b1, "base2": b2, "b3": b3, "mark": mark,
             "shadow": SH}, open(HERE / "v2_80.pkl", "wb"))
print("saved v2_80.pkl")
