"""반사실: 그 RED 플래그에 진입했다면, 뒤이은 반대(BLUE) 플래그를 H50 이 어떻게 처리했을까.

`small_whipsaw_hold.evaluate_hold()` 는 **순수 함수**다(상태를 읽지 않는다). 그래서 진입을
가정해도 판정 자체는 재생 없이 그대로 재현할 수 있다 — 판정 시점까지 완성된 3분봉만 넣으면 된다.

    python .../b2_h50_cf.py --day 20261030 --held UP_RED --at 13:06 13:09 13:12 13:15

해제(release) 판정도 같이 찍는다 — HOLD 가 걸렸다면 그 뒤 몇 봉 만에 풀렸을지 본다.
"""
import argparse
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

from app.trading.macd2 import config, signal_engine as SE, small_whipsaw_hold as H50  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402

SP = Path(os.environ.get("REPLAY_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"))
KST = config.KST


def hy_frame(days):
    fr = []
    for d in days:
        p = SP / "data" / f"replay_{d}_hynix_1m.csv"
        if p.exists():
            x = pd.read_csv(p)
            x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
            fr.append(x)
    hy = pd.concat(fr).drop_duplicates("datetime", keep="last")
    hy["datetime"] = hy["datetime"].dt.tz_localize(KST)
    return hy.sort_values("datetime").reset_index(drop=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", required=True)
    ap.add_argument("--held", default="UP_RED")
    ap.add_argument("--at", nargs="+", required=True, help="HH:MM 판정시각들")
    ap.add_argument("--prev", nargs=2, default=None)
    a = ap.parse_args()
    d = a.day
    held = Direction.UP_RED if a.held == "UP_RED" else Direction.DOWN_BLUE

    comp = sorted({f.stem.split("_")[1] for f in (SP / "data").glob("replay_*_hynix_1m.csv")})
    i = comp.index(d)
    days = a.prev or [comp[i - 2], comp[i - 1]]
    hy = hy_frame(list(days) + [d])

    print(f"\n{'='*118}")
    print(f"반사실: {d[:4]}-{d[4:6]}-{d[6:]} 에 {a.held} 포지션을 보유 중이었다면 — H50 판정")
    print(f"{'='*118}")
    print(f"{'판정시각':10s}{'HOLD?':8s}{'구조추세':10s}{'range%':>8s}{'추세일치':9s}{'범위OK':8s}  사유"
          f"   | 해제판정(release)")
    started = None
    brk = 0
    for hhmm in a.at:
        h, m = hhmm.split(":")
        now = datetime.strptime(f"{d} {h}:{m}", "%Y%m%d %H:%M").replace(tzinfo=KST)
        df1 = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(now)].reset_index(drop=True)
        bars = SE.resample_completed_3m(df1, now=now)
        hd = H50.evaluate_hold(bars, held, now=now)
        rel = ""
        if started is not None:
            rd = H50.evaluate_release(bars, held, started, now, trend_break_count=brk)
            brk = int(rd.trend_break_count)
            rel = (f"release={rd.should_release} trend={rd.trend} break={brk} "
                   f"elapsed={rd.elapsed_min:.0f}분 {rd.reason}"
                   if rd.elapsed_min is not None else f"release={rd.should_release} {rd.reason}")
        print(f"{hhmm:10s}{str(hd.should_hold):8s}{str(hd.trend):10s}"
              f"{(hd.range_pct if hd.range_pct is not None else float('nan')):>8.2f}"
              f"{str(hd.trend_ok):9s}{str(hd.range_ok):8s}  {hd.reason}   | {rel}")
        if hd.should_hold and started is None:
            started = now
    print(f"\n기준값: H50_RANGE_MAX_PCT={config.H50_RANGE_MAX_PCT} · "
          f"TREND_EMA {config.H50_TREND_EMA_FAST}/{config.H50_TREND_EMA_SLOW} · "
          f"RANGE_BARS={config.H50_RANGE_BARS} · TREND_BREAK_BARS={config.H50_TREND_BREAK_BARS} · "
          f"MAX_HOLD_MIN={config.H50_MAX_HOLD_MIN}")
    print("HOLD=True 면 그 반대 플래그로는 청산하지 않고 들고 간다(신호원장 H50_SMALL_WHIPSAW_HOLD).")


if __name__ == "__main__":
    main()
