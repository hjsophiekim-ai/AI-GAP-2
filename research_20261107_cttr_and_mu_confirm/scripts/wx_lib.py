"""후보 추출 공용 모듈 — production 코드는 읽기 전용 import 만 한다.

원본은 2026-11-06 WAIT-EXPAND 연구(research/macd2-mu-studies 계열)에서 쓴 모듈이고,
이 번들에서는 데이터 루트만 환경변수로 뺐다(계산은 한 줄도 바꾸지 않았다).

데이터 원천(날짜별 순차, 한 번에 하루만 메모리에 올린다):
  하이닉스 1분봉 : KIS 재생 1분봉 CSV(있으면) / 없으면 1초 스냅샷에서 1분 OHLC 재구성
  MU 1분봉       : ws_shadow mu/<날짜>/bars_1m.csv  (mu_assist.load_mu_1m 그대로)
  체결강도       : ws_shadow snapshots/<날짜>/000660.csv (exec_strength 정의 그대로)
  ETF 1분봉      : KIS 재생 long/inverse CSV / 없으면 스냅샷 재구성

환경변수(모두 선택 — 없으면 아래 기본 경로를 쓴다. HOWTO_REPRODUCE.md 참조):
  AXCT_REPO     저장소 루트
  AXCT_RT_NEW   ws_shadow 전개본 루트 (11/03~11/06) — 하위에 mu/ 와 snapshots/
  AXCT_RT_OLD   ws_shadow 전개본 루트 (10/15~11/02)
  AXCT_LED      production 원장 루트 — 하위에 YYYYMMDD/macd2_*_ledger.csv
  AXCT_BARS     KIS 재생 1분봉 CSV 디렉터리 목록 (os.pathsep 로 구분)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("MACD2_MU_ASSIST_ENABLED", "0")
sys.path.insert(0, os.environ.get("AXCT_REPO", r"G:\다른 컴퓨터\내 노트북 (2)\Desktop\AI-GAP 2"))

import pandas as pd

from app.trading.macd2 import config, exec_strength, mu_assist, signal_engine
from app.trading.macd2.models import Direction

KST = config.KST
HERE = Path(__file__).resolve().parent
_TMP = Path(r"C:\Users\KIMHYU~1\AppData\Local\Temp\claude")
_WX = _TMP / "G----------------2--Desktop-AI-GAP-2" / "b8fc9310-d582-4463-b146-e0f3b55feb3b" / "scratchpad"

RT_NEW = Path(os.environ.get("AXCT_RT_NEW") or (_WX / "rt"))          # 11/03~11/06
RT_OLD = Path(os.environ.get("AXCT_RT_OLD") or (_TMP / "arm3_sp" / "rt"))   # 10/15~11/02
LED_ROOT = Path(os.environ.get("AXCT_LED") or (_WX / "led"))          # 11/05~ production 원장
BAR_DIRS = ([Path(p) for p in os.environ["AXCT_BARS"].split(os.pathsep) if p]
            if os.environ.get("AXCT_BARS") else
            [_TMP / n / "data" for n in ("b3t_sp", "cttr_sp", "mufast_sp", "h50_sp")])

SYM = {"hynix": "000660", "long": config.LONG_SYMBOL, "inverse": config.INVERSE_SYMBOL}


def rt_root(day: str) -> Path:
    """ws_shadow 로거 데이터 루트 (mu/ 와 snapshots/ 의 부모)."""
    return RT_NEW if (RT_NEW / "snapshots" / day).exists() else RT_OLD


# ── 1분봉 ────────────────────────────────────────────────────────────────
def _replay_csv(kind: str, ymd: str) -> Path | None:
    for d in BAR_DIRS:
        p = d / f"replay_{ymd}_{kind}_1m.csv"
        if p.exists():
            return p
    return None


def _bars_from_snapshot(day: str, sym: str) -> pd.DataFrame:
    """1초 스냅샷 price → 1분 OHLC. 체결가(price)가 빈 행은 버린다."""
    p = rt_root(day) / "snapshots" / day / f"{sym}.csv"
    if not p.exists():
        return pd.DataFrame(columns=["datetime", "open", "high", "low", "close", "volume"])
    df = pd.read_csv(p, usecols=["ts", "price"])
    df = df[pd.to_numeric(df["price"], errors="coerce").notna()]
    df["price"] = df["price"].astype(float)
    df = df[df["price"] > 0]
    idx = pd.to_datetime(df["ts"]).dt.floor("1min")
    g = df.groupby(idx)["price"]
    out = pd.DataFrame({"open": g.first(), "high": g.max(), "low": g.min(), "close": g.last()})
    out["volume"] = 0
    out = out.reset_index().rename(columns={"ts": "datetime"})
    out["datetime"] = out["datetime"].dt.tz_localize(KST)
    return out


def load_1m(day: str, kind: str, prefer_snapshot: bool = False) -> pd.DataFrame:
    """kind = hynix | long | inverse. tz-aware KST 'datetime' 컬럼."""
    ymd = day.replace("-", "")
    p = None if prefer_snapshot else _replay_csv(kind, ymd)
    if p is not None:
        df = pd.read_csv(p)
        df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(KST)
        return df
    return _bars_from_snapshot(day, SYM[kind])


# ── 플래그 (production evaluate_macd_crossover 그대로) ────────────────────
def flags_of_day(bars_1m: pd.DataFrame, day: str) -> list[tuple[datetime, Direction]]:
    """그날 완성 3분봉을 하나씩 늘려가며 production 과 같은 순서로 제로크로스 플래그를 뽑는다."""
    if bars_1m is None or bars_1m.empty:
        return []
    clean, _ = signal_engine.exclude_preopen_padding_1m(bars_1m)
    last_dt = clean["datetime"].max()
    all_3m = signal_engine.resample_completed_3m(clean, last_dt + timedelta(minutes=3))
    out: list[tuple[datetime, Direction]] = []
    prev = None
    for i in range(config.EMA_SLOW + 2, len(all_3m) + 1):
        snap = signal_engine.calculate_macd(all_3m.iloc[:i])
        if snap is None:
            continue
        if snap.bar_dt.astimezone(KST).strftime("%Y-%m-%d") != day:
            continue
        d = signal_engine.evaluate_macd_crossover(snap, prev)
        if d != Direction.HOLD:
            prev = d
            out.append((snap.bar_dt.astimezone(KST), d))
    return out


def hy_gaps(bars_1m: pd.DataFrame, flag_bar_dt: datetime, direction: Direction):
    """worker._advance_mu_assist_entry_shadow 와 같은 정의의 (g_prev, g_flag, g_t2)."""
    clean, _ = signal_engine.exclude_preopen_padding_1m(bars_1m)
    bars_3m = signal_engine.resample_completed_3m(clean, flag_bar_dt + timedelta(minutes=3))
    snap = signal_engine.calculate_macd(bars_3m)
    if snap is None or snap.previous_diff is None or snap.current_diff is None:
        return None, None, None
    sign = 1.0 if direction == Direction.UP_RED else -1.0
    g_prev, g_flag = sign * float(snap.previous_diff), sign * float(snap.current_diff)
    t2 = flag_bar_dt + timedelta(minutes=5)
    rows = clean[clean["datetime"] < t2]
    last = rows[rows["datetime"] == t2 - timedelta(minutes=1)]
    if last.empty:
        return g_prev, g_flag, None
    prov = signal_engine.calculate_provisional_macd(bars_3m, rows, now=t2,
                                                    current_price=float(last["close"].iloc[-1]))
    g_t2 = None if prov is None else sign * float(prov.current_diff)
    return g_prev, g_flag, g_t2


# ── MU 상태 ──────────────────────────────────────────────────────────────
def mu_series(day: str):
    now = datetime.strptime(day, "%Y-%m-%d").replace(hour=15)
    return mu_assist.load_mu_1m(now, str(rt_root(day)))


def mu_status(series, direction: Direction, flag_bar_dt: datetime, gaps):
    pat = mu_assist.evaluate_pattern(series, direction, flag_bar_dt)
    st = mu_assist.hy_gap_state(*gaps)
    return mu_assist.entry_assist_status(pat["mu_pattern_state"], st), pat, st


# ── 체결강도 ─────────────────────────────────────────────────────────────
def cttr_at(day: str, when: datetime) -> float | None:
    """주의: exec_strength.mean_cttr 은 파일 끝 256KB 만 읽는다(라이브 append 전제).
    과거 시각 연구에는 cx_lib.DaySnap.mean_cttr(전체 스트리밍, 같은 정의)을 쓴다."""
    return exec_strength.mean_cttr(when, str(rt_root(day)))


# ── 사후 경로 ────────────────────────────────────────────────────────────
def forward(etf_1m: pd.DataFrame, t3: datetime, horizon_min=(15, 30), touch_pct=1.5):
    """T+3(= 플래그봉 +6분) 종가 기준 전방 수익률과 ±touch_pct 선터치.

    진입가는 T+3 시각에 끝난 1분봉 종가. 선터치는 이후 1분봉 high/low 로 본다
    (같은 봉에서 양쪽을 다 건드리면 BOTH_SAME_BAR)."""
    if etf_1m is None or etf_1m.empty:
        return {}
    done = etf_1m[etf_1m["datetime"] + pd.Timedelta(minutes=1) <= t3]
    if done.empty:
        return {}
    entry = float(done["close"].iloc[-1])
    fut = etf_1m[etf_1m["datetime"] >= t3]
    out = {"entry_px": entry}
    for h in horizon_min:
        end = t3 + timedelta(minutes=h)
        seg = etf_1m[etf_1m["datetime"] + pd.Timedelta(minutes=1) <= end]
        out[f"r{h}"] = round((float(seg["close"].iloc[-1]) / entry - 1) * 100, 3) if not seg.empty else None
    up, dn = entry * (1 + touch_pct / 100), entry * (1 - touch_pct / 100)
    first = "NONE"
    for _, row in fut.iterrows():
        hi, lo = float(row["high"]), float(row["low"])
        if hi >= up and lo <= dn:
            first = "BOTH_SAME_BAR"
            break
        if hi >= up:
            first = "UP"
            break
        if lo <= dn:
            first = "DOWN"
            break
    out["touch"] = first
    return out
