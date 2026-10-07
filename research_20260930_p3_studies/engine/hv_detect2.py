"""D2 HIGH-VOL 일 탐지기: production H50 과 같은 값(완성 3분봉 직전 20봉 고저폭, swh._range_pct) 이
처음 2.35% 를 넘는 완성봉 인식시각부터 그날 끝까지 ON. 새 임계값 없음 (H50_RANGE_MAX_PCT 재사용)."""
import pickle, sys
from pathlib import Path
import pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
from app.trading.macd2 import small_whipsaw_hold as swh, config
out = {}
for ctxf in ("_ctx83.pkl", "_ctx84.pkl"):
    c = pickle.load(open(HERE / ctxf, "rb")); b = c["hynix_bars_3m"].reset_index(drop=True)
    w = swh._prepare_bars(b)
    hi = b["high"].rolling(20).max(); lo = b["low"].rolling(20).min()
    rng = (hi - lo) / b["close"] * 100
    for i in range(20, len(b)):
        d = b["datetime"].iloc[i].strftime("%Y%m%d")
        if d in out: continue
        t = b["datetime"].iloc[i] + pd.Timedelta(minutes=3)
        if t.time() > config.NEW_ENTRY_CUTOFF: continue
        if rng.iloc[i] > float(config.H50_RANGE_MAX_PCT):
            out[d] = t
dates = list(pickle.load(open(HERE / "_ctx84.pkl", "rb"))["dates"])
pickle.dump(out, open(HERE / "hv_on2.pkl", "wb"))
W = dates[-81:]
print("최근 80영업일 + 0930:", sum(d in out for d in W[:-1]), "/ 80 일 ON")
print("9월:", [(d[4:], out[d].strftime("%H:%M")) for d in W if d.startswith("202609") and d in out])
print("0930:", out.get("20260930"))
