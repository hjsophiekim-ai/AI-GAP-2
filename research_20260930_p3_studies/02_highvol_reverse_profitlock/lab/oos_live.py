"""0930 실체결가 기준 반사실 (엔진이 11,045 봉가 진입이라 Q2/P3-RUNNER 를 재현 못 하므로).
실제: 09:06:05 947주 @11,015 / 09:09:34 189주 @11,146 (Q2) / 09:54:02 758주 @11,040 (전환 매도)
      09:54:03 인버스 1,807주 @5,770 / 09:58:26 @5,710 (B3 -1%)"""
import sys
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")
from app.trading.macd2.worker import _net_return_pct
from app.trading.macd2.models import Direction
from app.trading.macd2 import signal_engine as se, market_data as md, config
import tregime as tr
from app.trading.macd2 import small_whipsaw_hold as swh
L = pd.read_csv(HERE / "cache84/replay_20260930_long_1m.csv", parse_dates=["datetime"]).set_index("datetime")
E, Q = 11015.0, 758
net = lambda px: _net_return_pct("0193T0", E, float(px), Q)
# MFE 경로 (1분봉 고가 = 틱 근사)
arm = None
for ts, r in L.loc["2026-09-30 09:10":].iterrows():
    if arm is None and net(r["high"]) >= 2.0:
        arm = ts; print("R1 ARM (+2% 최초, 1분 고가)", ts.strftime("%H:%M"), "high", r["high"], f"net {net(r['high']):+.3f}")
mfe = max(net(h) for h in L.loc["2026-09-30 09:10":"2026-09-30 09:54", "high"])
print(f"runner MFE(09:10~09:54) {mfe:+.3f}")
# 완성봉 net (3분 경계 = 봉 인식시각의 직전 1분봉 종가)
r1 = None
for t in pd.date_range("2026-09-30 09:12", "2026-09-30 10:33", freq="3min"):
    m = t - pd.Timedelta(minutes=1)
    if m not in L.index: continue
    n = net(L.loc[m, "close"])
    if arm is not None and t > arm and n <= 1.0 and r1 is None:
        r1 = (t, L.loc[m, "close"], n)
print("R1 floor 청산:", None if r1 is None else f"{r1[0].strftime('%H:%M')} @{r1[1]:,.0f} net {r1[2]:+.3f}")
# 하이닉스 추세 (production 프레임 재구성본)
H = pd.read_csv(HERE.parent.parent / "hynix_1m_0930.csv", index_col=0, parse_dates=["datetime"])
H["datetime"] = pd.to_datetime(H["datetime"], utc=True).dt.tz_convert(config.KST)
now = pd.Timestamp("2026-09-30 10:21:30", tz=config.KST).to_pydatetime()
b3 = se.resample_completed_3m(H, now); b3, _ = md.filter_complete_3m_bars(b3, H); b3 = b3.reset_index(drop=True)
for lab_, end in (("09:54 확정(09:51봉까지)", "09:51"), ("1봉 뒤 09:57(09:54봉까지)", "09:54")):
    i = int(b3.index[b3["datetime"].dt.strftime("%Y-%m-%d %H:%M") == f"2026-09-30 {end}"][0])
    s = tr.snapshot(b3.iloc[: i + 1], Direction.UP_RED)
    h = swh.evaluate_hold(b3.iloc[: i + 1], Direction.UP_RED)
    print(f"{lab_}: N1 추세 ok={s.ok} close {s.close:,.0f} EMA20 {s.ema_fast:,.0f} EMA50 {s.ema_slow:,.0f} "
          f"slope {s.ema_slow_slope:+.1f} | H50 {h.reason} range {h.range_pct:.3f}%")
last = L.index.max()
print(f"R2 유지 시 레버리지 {last.strftime('%H:%M')} 종가 {L['close'].iloc[-1]:,.0f} net {net(L['close'].iloc[-1]):+.3f} "
      f"| 09:54~ 최저 완성봉 net {min(net(L.loc[t - pd.Timedelta(minutes=1), 'close']) for t in pd.date_range('2026-09-30 09:57', last, freq='3min') if t - pd.Timedelta(minutes=1) in L.index):+.3f}")
krw = lambda px: (px - E) * Q
print(f"[원화, 수수료 전] 실제 잔량 {krw(11040):+,.0f} | R1 {krw(r1[1]) if r1 else float('nan'):+,.0f} | "
      f"R2(10:34 평가) {krw(L['close'].iloc[-1]):+,.0f} | 인버스 실제 {(5710-5770)*1807:+,.0f}")
