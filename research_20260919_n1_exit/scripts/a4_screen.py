"""A4 — milestone 시점 조건별 분리력 스크린 (규칙 설계 근거). 임계값 탐색 아님:
전부 저장소에 이미 있는 이진 조건이거나 부호(>0) 판정이다."""
import sys
sys.stdout.reconfigure(encoding="utf-8")
import numpy as np, pandas as pd
from pathlib import Path
HERE = Path(__file__).resolve().parent
df = pd.read_csv(HERE / "ms78_features.csv")

CONDS = [
    ("regime_ok",          lambda d: d.regime_ok),
    ("gap_pos",            lambda d: d.gap_pos),
    ("gap_rising",         lambda d: d.gap_rising),
    ("gap_rising2",        lambda d: d.gap_rising2),
    ("gap_pos&rising",     lambda d: d.gap_pos & d.gap_rising),
    ("regime&gap_rising",  lambda d: d.regime_ok & d.gap_rising),
    ("regime&gap_pos",     lambda d: d.regime_ok & d.gap_pos),
    ("vwap_ok",            lambda d: d.vwap_ok),
    ("regime&vwap",        lambda d: d.regime_ok & d.vwap_ok),
    ("ema50_slope>0",      lambda d: d.ema50_slope_pct > 0),
    ("ema20_slope>0",      lambda d: d.ema20_slope_pct > 0),
    ("close>ema20",        lambda d: d.close_vs_ema20_pct > 0),
    ("ema_major_spread>0", lambda d: d.ema_spread_major > 0),
    ("no_opp_flag",        lambda d: d.opp_flags == 0),
    ("not_h50_held",       lambda d: ~d.h50_held),
    ("MORNING",            lambda d: d.session == "MORNING"),
    ("not_entry_chop",     lambda d: ~d.entry_chop),
    ("trend_at_entry",     lambda d: d.trend_at_entry),
    ("vol_ratio>1",        lambda d: d.vol_ratio > 1.0),
    ("mins<=30",           lambda d: d.mins <= 30),
    ("mins<=60",           lambda d: d.mins <= 60),
    ("mae>=-1.0",          lambda d: d.mae >= -1.0),
]

for lv in (3.0, 4.0):
    d = df[df.level == lv].copy()
    d["hold_gain"] = d.y_net - lv          # 지금 청산 대비 계속 보유의 이득
    print(f"\n{'='*104}\nlevel +{lv}%  n={len(d)}  runner(peak>=8)={int(d.y_runner.sum())}  "
          f"평균 hold_gain={d.hold_gain.mean():+.3f}  (음수면 그 자리 익절이 유리)")
    print(f"{'조건':22s} {'n_T':>4s} {'run_T':>5s} {'gain_T':>7s} {'n_F':>4s} {'run_F':>5s} {'gain_F':>7s} {'Δgain':>7s} {'F쪽구제':>8s}")
    for name, fn in CONDS:
        mT = fn(d).fillna(False).astype(bool)
        T, F = d[mT], d[~mT]
        if len(T) == 0 or len(F) == 0:
            continue
        # F 쪽에서 그 자리 익절하면 얼마나 벌었나(= -sum(hold_gain_F))
        saved = -F.hold_gain.sum()
        print(f"{name:22s} {len(T):4d} {int(T.y_runner.sum()):5d} {T.hold_gain.mean():+7.3f} "
              f"{len(F):4d} {int(F.y_runner.sum()):5d} {F.hold_gain.mean():+7.3f} "
              f"{T.hold_gain.mean()-F.hold_gain.mean():+7.3f} {saved:+8.2f}")

    print("\n  연속변수 — runner(peak>=8) vs 나머지 평균")
    for cvar in ("mins", "bars", "mae", "ema_spread_pct", "ema50_slope_pct", "ema20_slope_pct",
                 "close_vs_ema20_pct", "gap", "gap_vs_max", "ema_spread_major",
                 "vwap_dev_pct", "vol_ratio", "opp_flags", "tq"):
        r, n = d[d.y_runner][cvar], d[~d.y_runner][cvar]
        print(f"    {cvar:20s} runner={r.mean():8.3f}  기타={n.mean():8.3f}  "
              f"중앙값 {r.median():8.3f} / {n.median():8.3f}")
