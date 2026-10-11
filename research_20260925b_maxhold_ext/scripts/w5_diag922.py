"""W5 - 9/22 12:15 DOWN_BLUE 의 봉별 조건값 추적 (12:15~13:21)."""
import sys, pickle
from pathlib import Path
import numpy as np, pandas as pd
sys.stdout.reconfigure(encoding="utf-8")
pd.set_option("display.width", 320)
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import hengine5 as H
import _tmp_20260903_chop_adaptive_exit_train_oos as ce
import axlib as A
ce.CACHE_DIR = Path(r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2\data\cache")
H._CTX_CACHE = HERE / "_ctx81.pkl"
ctx = H.build_ctx(81)
b = ctx.hynix_bars_3m.reset_index(drop=True)
dt = pd.to_datetime(b["datetime"])
date = dt.dt.strftime("%Y%m%d").values
tm = dt.dt.strftime("%H:%M").values
c = b["close"].astype(float).values
hi = b["high"].astype(float).values
lo = b["low"].astype(float).values
vol = b["volume"].astype(float).values
ef = b["close"].ewm(span=12, adjust=False).mean()
es = b["close"].ewm(span=26, adjust=False).mean()
gap = (ef - es - (ef - es).ewm(span=9, adjust=False).mean()).values
e20 = b["close"].ewm(span=20, adjust=False).mean().values
e50 = b["close"].ewm(span=50, adjust=False).mean().values
spr = e20 - e50
vw = np.full(len(b), np.nan); cur, pv, pvv = None, 0.0, 0.0
tp3 = (hi + lo + c) / 3.0
for i in range(len(b)):
    if date[i] != cur:
        cur, pv, pvv = date[i], 0.0, 0.0
    v = vol[i] if vol[i] > 0 else 1.0
    pv += tp3[i] * v; pvv += v
    vw[i] = pv / pvv
m = (date == "20260922") & (tm >= "12:12") & (tm <= "13:21")
idx = np.where(m)[0]
sym = "0197X0"
q = ctx.quotes[sym]
ent_px = q.at(pd.Timestamp("2026-09-22T12:15:00+09:00"))
print("진입가 %s @12:15 = %s (DOWN_BLUE, 인버스 ETF)" % (sym, ent_px))
rows = []
for i in idx:
    t = pd.Timestamp(b["datetime"].iloc[i]) + pd.Timedelta(minutes=3)
    px = q.at(t)
    net = (px - ent_px) / ent_px * 100 if (px and ent_px) else np.nan
    dg = gap[i] - gap[i - 1] if date[i - 1] == date[i] else np.nan
    ds = spr[i] - spr[i - 1] if date[i - 1] == date[i] else np.nan
    p0 = q.at(t - pd.Timedelta(minutes=3))
    rows.append(dict(봉=tm[i], 판정시각=str(t)[11:16], ETF가=px,
                     net=round(net, 3) if net == net else None,
                     hynix=c[i],
                     dgap=round(float(dg), 2) if dg == dg else None,
                     gap확대_BLUE=(bool(-dg > 0) if dg == dg else None),
                     dspread=round(float(ds), 2) if ds == ds else None,
                     spread확대_BLUE=(bool(-ds > 0) if ds == ds else None),
                     vwap우호_BLUE=bool(c[i] < vw[i]),
                     ETF추종=(bool(px >= p0) if (px and p0) else None)))
D = pd.DataFrame(rows)
print(D.to_string(index=False))
print("\n※ DOWN_BLUE 는 기초자산 하락에 베팅 -> gap/spread 가 **음의 방향으로** 확대돼야 우호.")
print("※ B3 max-hold 20분 만료 = 12:35 판정 (봉 12:33)")
