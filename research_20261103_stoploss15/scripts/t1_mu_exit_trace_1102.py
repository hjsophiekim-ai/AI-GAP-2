"""11/02 13:06 BLUE 반대플래그 — MU ASSIST H50 gap 조기청산의 분기값 재구성 (READ-ONLY).

production 함수만 호출한다:
  mu_assist.load_mu_1m / evaluate_pattern / hy_gap_state / in_t2_window
  signal_engine.resample_completed_3m / calculate_provisional_macd
  small_whipsaw_hold.evaluate_hold
"""
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(os.environ["WT"])
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app.trading.macd2 import config, mu_assist, signal_engine as SE, small_whipsaw_hold as H50  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

SP = Path(os.environ["REPLAY_SP"])
RT = os.environ["ARM3_RT"]
KST = config.KST
DAY = "20261102"
FLAG = datetime.strptime(f"{DAY} 13:06", "%Y%m%d %H:%M").replace(tzinfo=KST)
FLAG_DIR = Direction.DOWN_BLUE          # 반대플래그 방향
HELD_DIR = Direction.UP_RED             # 보유(레버리지) 방향


def hy_1m():
    fr = []
    for d in ("20261029", "20261030", DAY):
        p = SP / "data" / f"replay_{d}_hynix_1m.csv"
        if p.exists():
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            fr.append(x)
    hy = pd.concat(fr).drop_duplicates("datetime", keep="last")
    hy["datetime"] = hy["datetime"].dt.tz_localize(KST)
    return hy.sort_values("datetime").reset_index(drop=True)


hy = hy_1m()
sign = 1.0 if FLAG_DIR == Direction.UP_RED else -1.0

print(f"[대상] {DAY} 플래그봉 {FLAG:%H:%M} {FLAG_DIR.value} / 보유 {HELD_DIR.value}")
print(f"  T+2 창 = F+5분+{config.MU_ASSIST_T2_GRACE_SEC}초 ~ F+6분 "
      f"= {FLAG+timedelta(minutes=5, seconds=config.MU_ASSIST_T2_GRACE_SEC):%H:%M:%S} ~ {FLAG+timedelta(minutes=6):%H:%M:%S}")
print(f"  T+3 = {FLAG+timedelta(minutes=6):%H:%M}\n")

mu = mu_assist.load_mu_1m(FLAG, data_dir=RT)
print(f"[MU 1분봉] {'없음' if mu is None else f'{len(mu)}개  {mu.index[0]} ~ {mu.index[-1]}'}"
      f"   last_real={None if mu is None else mu.attrs.get('last_real_minute')}")

pat = mu_assist.evaluate_pattern(mu, FLAG_DIR, FLAG)
print("\n[MU 패턴] (반대플래그 DOWN_BLUE 방향 기준)")
for k, v in pat.items():
    print(f"   {k:24s} {v}")

print("\n[tick 별 분기값]")
hdr = (f"  {'시각':9s}{'T+2창':7s}{'H50 HOLD':10s}{'gap_flag':>10s}{'gap_t2':>10s}"
       f"{'expanding':>11s}  {'MU state':10s} -> 판정")
print(hdr)
for hhmmss in ("13:09:00", "13:10:20", "13:11:00", "13:11:20", "13:11:40", "13:12:00"):
    now = datetime.strptime(f"{DAY} {hhmmss}", "%Y%m%d %H:%M:%S").replace(tzinfo=KST)
    in_win = mu_assist.in_t2_window(FLAG, now)
    df1 = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)].reset_index(drop=True)
    bars = SE.resample_completed_3m(df1, now=now)
    hold = H50.evaluate_hold(bars, HELD_DIR, now=now)
    px = float(df1["close"].iloc[-1]) if len(df1) else 0.0
    gap_flag = gap_t2 = float("nan")
    exp = None
    if bars is not None and not bars.empty and px > 0:
        # macd_snap = 마지막 완성 3분봉(= 플래그봉) 의 gap
        snap = SE.calculate_provisional_macd(bars.iloc[:-1], df1, now=now, current_price=float(bars["close"].iloc[-1]))
        full = SE.calculate_provisional_macd(bars, df1, now=now, current_price=px)
        if snap is not None and full is not None:
            gap_flag = sign * float(snap.current_diff)
            gap_t2 = sign * float(full.current_diff)
            exp = bool(gap_t2 > 0 and gap_t2 > gap_flag)
    label = ("창밖" if not in_win else
             "H50 미HOLD->기존경로" if not hold.should_hold else
             "HOLD_GAP_NOT_EXPANDING" if not exp else
             "HOLD_MU_UNAVAILABLE" if pat["mu_pattern_state"] == mu_assist.PATTERN_UNAVAILABLE else
             "HOLD_MU_NOT_CONFIRM" if pat["mu_pattern_state"] != mu_assist.PATTERN_CONFIRM else "EXIT")
    print(f"  {hhmmss:9s}{str(in_win):7s}{str(hold.should_hold):10s}{gap_flag:>10.4f}{gap_t2:>10.4f}"
          f"{str(exp):>11s}  {pat['mu_pattern_state']:10s} -> {label}")

print("\n[참고] 보유방향(UP_RED) 기준 gap 도 같이:")
for hhmmss in ("13:09:00", "13:12:00"):
    now = datetime.strptime(f"{DAY} {hhmmss}", "%Y%m%d %H:%M:%S").replace(tzinfo=KST)
    df1 = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)].reset_index(drop=True)
    bars = SE.resample_completed_3m(df1, now=now)
    px = float(df1["close"].iloc[-1])
    full = SE.calculate_provisional_macd(bars, df1, now=now, current_price=px)
    print(f"  {hhmmss}  UP_RED gap_now = {float(full.current_diff):+.4f}")
