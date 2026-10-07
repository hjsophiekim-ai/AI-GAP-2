"""0930 OOS — 실체결가 기준 9개 후보 반사실 (집계 제외). 11:16 까지 데이터. READ-ONLY."""
import sys
from pathlib import Path
import pandas as pd
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "proj")); sys.path.insert(0, str(HERE / "proj" / "scripts"))
sys.stdout.reconfigure(encoding="utf-8")
from app.trading.macd2.worker import _net_return_pct
from app.trading.macd2.models import Direction
from app.trading.macd2 import signal_engine as se, config
import tregime as tr
KST = config.KST
Lg = pd.read_csv(HERE / "oos/replay_20260930_long_1m.csv", parse_dates=["datetime"]).set_index("datetime")
h = pd.concat([pd.read_csv(HERE / "cache84/replay_20260929_hynix_1m.csv", parse_dates=["datetime"]),
               pd.read_csv(HERE / "oos/replay_20260930_hynix_1m.csv", parse_dates=["datetime"])])
h["datetime"] = h["datetime"].dt.tz_localize(KST)
now = pd.Timestamp("2026-09-30 11:16:30", tz=KST).to_pydatetime()
b = se.resample_completed_3m(h.reset_index(drop=True), now).reset_index(drop=True)
prev = None; flags = []
for i in range(len(b)):
    s = se.calculate_macd(b.iloc[: i + 1])
    if s is None: continue
    d = se.evaluate_macd_crossover(s, prev)
    if d in (Direction.UP_RED, Direction.DOWN_BLUE):
        if str(b["datetime"].iloc[i])[:10] == "2026-09-30": flags.append((str(b["datetime"].iloc[i])[11:16], d.value))
        prev = d
print("오늘 플래그(3분봉 시작시각):", flags)
def snap(end, d):
    i = int(b.index[b["datetime"].dt.strftime("%H:%M").eq(end) & b["datetime"].dt.strftime("%Y%m%d").eq("20260930")][0])
    return tr.snapshot(b.iloc[: i + 1], d).ok
print("09:57 판정(09:54봉까지) 인버스 방향 N1 추세:", snap("09:54", Direction.DOWN_BLUE), "/ 레버리지 방향:", snap("09:54", Direction.UP_RED))
E = 11015.0
net = lambda px, q=758: _net_return_pct("0193T0", E, float(px), q)
bars = [t for t in pd.date_range("2026-09-30 09:57", "2026-09-30 11:15", freq="3min") if t - pd.Timedelta(minutes=1) in Lg.index]
cl = [(t, float(Lg.loc[t - pd.Timedelta(minutes=1), "close"])) for t in bars]
mn = min(cl, key=lambda x: x[1]); mx = Lg.loc["2026-09-30 09:54":, "high"].max()
last = float(Lg["close"].iloc[-1])
print(f"09:54 이후 레버리지: 최저 완성봉 {mn[0].strftime('%H:%M')} @{mn[1]:,.0f} ({net(mn[1]):+.2f}%) · 최고 {mx:,.0f} ({net(mx):+.2f}%) · "
      f"11:16 @{last:,.0f} ({net(last):+.2f}%)  [N1 손절 −1.3% / TP1 +3.0% 미도달이면 계속 보유]")
Q2 = (11146 - E) * 189; INV = (5710 - 5770) * 1807
PL = 11105.0   # 09:48 완성봉 (ARM 09:29 이후 첫 +1% 이하)
def row(name, lock_q, a_keep, b_exo):
    rem = 758 - lock_q
    lock = (PL - E) * lock_q
    if a_keep:
        restv = (last - E) * rem; how = f"{rem}주 계속 보유, 11:16 평가 @{last:,.0f} (미실현)"; inv = 0; invs = "진입 안 함(전환 무시)"
    else:
        restv = (11040 - E) * rem; how = f"{rem}주 09:54 @11,040"
        inv = 0 if b_exo else INV; invs = "1봉 뒤 인버스 추세 미확인 → 진입 취소" if b_exo else "09:54 @5,770 → 09:58 @5,710 손절"
    tot = Q2 + lock + restv + inv
    print(f"| {name} | +{Q2:,.0f} | {'-' if not lock_q else f'{lock_q}주 09:48 @11,105 {lock:+,.0f}'} | {how} {restv:+,.0f} | {invs} {inv:+,.0f} | **{tot:+,.0f}** |")
print("\n| 후보 | Q2 189주 | 부분보호 | 레버리지 잔량 | 인버스 | 하루 합(수수료 전) |\n|---|---|---|---|---|---|")
row("R0 실제", 0, False, False); row("A R2", 0, True, False); row("B EXIT-ONLY", 0, False, True)
row("C30", 227, False, False); row("C50", 379, False, False)
row("A+C30", 227, True, False); row("A+C50", 379, True, False)
row("B+C30", 227, False, True); row("B+C50", 379, False, True)
