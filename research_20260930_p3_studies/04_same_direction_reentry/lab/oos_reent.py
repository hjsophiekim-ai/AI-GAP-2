"""0930 OOS — 실제 09:58:26 인버스 B3 손절 기준 동일방향 재진입 (집계 제외). READ-ONLY."""
import sys
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts")); sys.stdout.reconfigure(encoding="utf-8")
from app.trading.macd2.worker import _net_return_pct
from app.trading.macd2.models import Direction
from app.trading.macd2 import signal_engine as se, market_data as md, config
import tregime as tr
h = pd.concat([pd.read_csv(HERE / "cache84/replay_20260929_hynix_1m.csv", parse_dates=["datetime"]),
               pd.read_csv(HERE / "oos/replay_20260930_hynix_1m.csv", parse_dates=["datetime"])])
h["datetime"] = h["datetime"].dt.tz_localize(config.KST); h = h.reset_index(drop=True)
now = h["datetime"].iloc[-1].to_pydatetime() + pd.Timedelta(minutes=1)
b = se.resample_completed_3m(h, now); b, _ = md.filter_complete_3m_bars(b, h); b = b.reset_index(drop=True)
inv = pd.read_csv(HERE / "oos/replay_20260930_inverse_1m.csv", parse_dates=["datetime"]).set_index("datetime")
sl = pd.Timestamp("2026-09-30 09:58:26", tz=config.KST)
dec = None
for i in range(len(b)):
    ts = b["datetime"].iloc[i]
    if ts >= sl.floor("3min") + pd.Timedelta(minutes=3) - pd.Timedelta(minutes=3) and ts >= sl - pd.Timedelta(seconds=0) or ts > sl:
        if ts.strftime("%Y%m%d") == "20260930" and tr.snapshot(b.iloc[: i + 1], Direction.DOWN_BLUE).ok:
            dec = ts + pd.Timedelta(minutes=3); break
print("재진입 조건 첫 충족(완성봉 인식시각):", dec)
at = dec.tz_localize(None); e = float(inv.loc[at, "open"])
net = lambda px: _net_return_pct("0197X0", e, float(px), 1000)
path = inv.loc[at:]
first_tp = first_sl = None
for ts, r in path.iterrows():
    if first_sl is None and net(r["low"]) <= -1.0: first_sl = (ts, r["low"])
    if first_tp is None and net(r["high"]) >= 1.0: first_tp = (ts, r["high"])
    if first_tp or first_sl: break
mfe = max(net(x) for x in path["high"]); mae = min(net(x) for x in path["low"])
print(f"재진입가 {e:,.0f} @ {at:%H:%M} · 이후 MFE {mfe:+.2f}% · MAE {mae:+.2f}% · 데이터 끝 {path.index.max():%H:%M} 종가 {path['close'].iloc[-1]:,.0f} ({net(path['close'].iloc[-1]):+.2f}%)")
print("B3(CHOP 가정) 첫 이벤트:", "TP +1% " + f"{first_tp[0]:%H:%M} ({(first_tp[0]-at).total_seconds()/60:.0f}분)" if first_tp else "", "SL -1% " + f"{first_sl[0]:%H:%M}" if first_sl else "")
