"""A3 — milestone 시점의 인과적 특징 테이블. 미래값 없음.

EMA/MACD/VWAP 은 전부 재귀(ewm adjust=False)·일자내 누적이라 전체 프레임에서
한 번 계산한 값의 i 번째 원소 == prefix[:i+1] 로 계산한 마지막 원소다. 검증도 한다.
"""
import pickle
import numpy as np, pandas as pd
import axlib as A
import tregime as tr
from app.trading.macd2 import config
from app.trading.macd2.major_flag_filter import _prepare_bars, _ema, _session_vwap
from app.trading.macd2.models import Direction
from app.trading.macd2 import signal_engine as se

D = pickle.load(open(A.HERE / "ms78_N1.pkl", "rb"))
c = A.ctx(78)
bars = c.hynix_bars_3m
work = _prepare_bars(bars)
assert len(work) == len(bars), (len(work), len(bars))

close = work["close"].astype(float)
ema20 = _ema(close, config.H50_TREND_EMA_FAST)
ema50 = _ema(close, config.H50_TREND_EMA_SLOW)
mser = se.calculate_macd_series(work)
gap = (mser["macd"] - mser["signal"]).astype(float)
vwap = _session_vwap(work)
vol = work["volume"].astype(float)
vol_ma = vol.rolling(20, min_periods=5).mean()
ema10m = _ema(close, config.MAJOR_EMA_FAST)
ema20m = _ema(close, config.MAJOR_EMA_SLOW)

# --- 인과성 검증: 임의 인덱스 몇 개에서 prefix 계산과 일치하는가 -------------
for i in (200, 1500, 6000, 11000):
    w2 = _prepare_bars(bars.iloc[: i + 1])
    assert abs(float(_ema(w2["close"].astype(float), 20).iloc[-1]) - float(ema20.iloc[i])) < 1e-9
    m2 = se.calculate_macd_series(w2)
    assert abs(float((m2["macd"] - m2["signal"]).iloc[-1]) - float(gap.iloc[i])) < 1e-9
    assert abs(float(_session_vwap(w2).iloc[-1]) - float(vwap.iloc[i])) < 1e-9
print("인과성 검증 OK (prefix == 전체계산 i번째)")

flag_idx = sorted(c.flags_by_idx)
trades = {(t["date"], t["entry_time"]): t for t in D["trades"]}


def sgn(d):
    return 1.0 if d == "UP_RED" else -1.0


rows = []
for m in D["ms"]:
    i = m["idx"]
    s = sgn(m["direction"])
    hd = Direction.UP_RED if m["direction"] == "UP_RED" else Direction.DOWN_BLUE
    snap = tr.snapshot(bars.iloc[: i + 1], hd)
    cl = float(close.iloc[i])
    g = float(gap.iloc[i]) * s
    g1 = float(gap.iloc[i - 1]) * s if i > 0 else np.nan
    g2 = float(gap.iloc[i - 2]) * s if i > 1 else np.nan
    e_i = m["entry_idx"]
    gmax = float((gap.iloc[e_i:i + 1] * s).max()) if i >= e_i else g
    vw = float(vwap.iloc[i])
    t = trades[(m["date"], m["entry_time"])]
    opp = Direction.DOWN_BLUE if hd == Direction.UP_RED else Direction.UP_RED
    opp_flags = sum(1 for j in flag_idx if e_i <= j <= i and c.flags_by_idx[j] == opp)
    same_flags = sum(1 for j in flag_idx if e_i <= j <= i and c.flags_by_idx[j] == hd)
    rows.append({
        "date": m["date"], "entry_time": m["entry_time"], "session": m["session"],
        "direction": m["direction"], "level": m["level"], "idx": i,
        "mins": m["mins"], "bars": m["bars"], "mae": m["mae_so_far"],
        # 구조 (H50 과 같은 EMA20/50)
        "regime_ok": bool(snap.ok),
        "ema_spread_pct": (float(ema20.iloc[i]) - float(ema50.iloc[i])) / cl * 100.0 * s,
        "ema50_slope_pct": float(ema50.iloc[i] - ema50.iloc[i - 1]) / cl * 100.0 * s,
        "ema20_slope_pct": float(ema20.iloc[i] - ema20.iloc[i - 1]) / cl * 100.0 * s,
        "close_vs_ema20_pct": (cl - float(ema20.iloc[i])) / cl * 100.0 * s,
        # MACD (production _gap_series 와 동일)
        "gap": g, "gap_rising": bool(g > g1), "gap_rising2": bool(g > g1 > g2),
        "gap_vs_max": g / gmax if gmax > 0 else np.nan,
        "gap_pos": bool(g > 0),
        # MAJOR EMA10/20 spread — whipsaw-watch 가 쓰는 것과 같은 스팬
        "ema_spread_major": (float(ema10m.iloc[i]) - float(ema20m.iloc[i])) / cl * 100.0 * s,
        # VWAP (production _session_vwap)
        "vwap_dev_pct": (cl / vw - 1.0) * 100.0 * s if vw > 0 else np.nan,
        "vwap_ok": bool((cl > vw) if s > 0 else (cl < vw)) if vw > 0 else False,
        # 거래량
        "vol_ratio": float(vol.iloc[i] / vol_ma.iloc[i]) if vol_ma.iloc[i] > 0 else np.nan,
        # 반대 플래그 / whipsaw
        "opp_flags": opp_flags, "same_flags": same_flags,
        "h50_held": bool(t["h50_held"]),
        # 진입시점 라벨
        "entry_chop": bool(t["entry_chop"]), "tq": t["tq"], "tp1_done": m["tp1_done"],
        "trend_at_entry": bool(t["trend_at_entry"]),
        # --- 결과 (라벨 전용, 규칙 입력 아님) ---
        "y_peak": float(t["peak_net_pct"]), "y_net": float(t["net_pct"]),
        "y_runner": bool(t["peak_net_pct"] >= 8.0),
        "y_reason": t["exit_reason"], "w1a": float(t["w1a"]),
    })

df = pd.DataFrame(rows)
df.to_csv(A.HERE / "ms78_features.csv", index=False, encoding="utf-8-sig")
print(f"\nmilestone {len(df)}건  (level별: " +
      ", ".join(f"{k}={v}" for k, v in df.level.value_counts().sort_index().items()) + ")")
print(df.groupby("level").agg(n=("y_peak", "size"), runner=("y_runner", "sum"),
                              peak=("y_peak", "mean"), net=("y_net", "mean")).round(3).to_string())
print("저장: ms78_features.csv")
