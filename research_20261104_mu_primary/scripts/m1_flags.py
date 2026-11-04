"""MU 플래그 전수 열거 + 기존 production 판정기 전부 부착 + ETF 결과 라벨 (research only).

production 무수정 · 읽기 전용. 재생 불필요 — 전부 순수 함수 + 저장된 1분봉/스냅샷.

원신호 정의 (production 과 동일한 계산식을 MU 1분봉에 그대로 적용):
  MU 1분봉 종가 -> MACD(12,26,9, adjust=False) hist (= mu_assist._macd_hist, config 상수 그대로)
  플래그 = signal_engine.evaluate_macd_crossover 의 제로크로스 규칙
      prev_diff <= 0 and cur_diff > 0  -> RED
      prev_diff >= 0 and cur_diff < 0  -> BLUE
      직전 확정방향과 같으면 억제(HOLD)   <- 동일한 repeat suppression
  production 이 3분 *형성봉* 을 매 tick 평가하는 것과 달리, MU 는 완성 1분봉만 있으므로
  플래그 인지 시각 = 그 1분봉 종료 시각(F_end). 미래 데이터 없음.

판정 시각 T = F_end + 2분  (MU Assist 의 T+2 와 같은 자리: D=F_end, D+2)

부착하는 판정 (전부 기존 함수 호출, 새 임계값 없음):
  mu_assist.evaluate_pattern   -> CONFIRM / STALL / DIVERGE / UNAVAILABLE
  exec_strength.evaluate       -> MATCH / MISMATCH / UNAVAILABLE (하이닉스 체결강도, 기준 100)
  time_window_filter._gap_series / calculate_flag_quality_score 재료
  major_flag_filter.evaluate_major_flag (점수·VWAP·breakout)
  teg_gate.evaluate_teg        (MU 판정봉 기준 하이닉스 3분봉에 적용)
  trend_persistence_filter.evaluate_trend_persistence
  chop_defense._trend_ok / _path_is_chop / _macd_gap_3m_last

결과 라벨 (production 임계값만 사용, 새 값 생성 금지):
  ETF = RED -> 0193T0(long), BLUE -> 0197X0(inverse)
  진입가 = T 에 시작하는 ETF 1분봉 시가 (미래 없음)
  TP1 = +3.0% (config.MORNING_TP1), SL = -1.3% (config.X2LITE_MORNING_STOP_LOSS)
  path = 당일 1분봉 고가/저가로 TP1 선터치 / SL 선터치 / 무승부(둘 다 미터치)
  mfe/mae = 30분 이내 최대 유·불리 (순수익률, 왕복 수수료 차감)
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.stdout.reconfigure(encoding="utf-8")

RT = Path(os.environ.get("ARM3_RT", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/arm3_sp/rt"))
SP = Path(os.environ.get("REPLAY_SP", r"C:/Users/KIMHYU~1/AppData/Local/Temp/claude/b3t_sp"))
OUT = ROOT / "research_20261104_mu_primary" / "out"

os.environ.setdefault("MACD2_MU_ASSIST_DATA_DIR", str(RT))
os.environ.setdefault("MACD2_EXEC_STRENGTH_DATA_DIR", str(RT))

from app.trading.macd2 import (  # noqa: E402
    chop_defense, config, exec_strength, major_flag_filter, mu_assist,
    signal_engine, teg_gate, time_window_filter, trend_persistence_filter,
)
from app.trading.macd2.models import Direction  # noqa: E402

FEE = 0.000036                       # 편도 (왕복 2회 차감)
_CTTR: dict[str, pd.Series | None] = {}


def mean_cttr_full(now: datetime) -> float | None:
    """exec_strength.mean_cttr 와 **같은 정의**([now-60s, now) 평균). 과거 시각을 보려면
    파일 꼬리(_TAIL_BYTES)가 아니라 전체를 읽어야 한다 — 판정 정의는 그대로다."""
    day = exec_strength._naive_kst(now).strftime("%Y-%m-%d")
    if day not in _CTTR:
        f = RT / "snapshots" / day / f"{exec_strength.SYMBOL}.csv"
        if not f.exists():
            _CTTR[day] = None
        else:
            x = pd.read_csv(f, usecols=["ts", "cttr"])
            x["ts"] = pd.to_datetime(x["ts"])
            _CTTR[day] = x.dropna(subset=["cttr"]).set_index("ts")["cttr"].sort_index()
    s = _CTTR[day]
    if s is None or s.empty:
        return None
    end = exec_strength._naive_kst(now)
    w = s[(s.index >= end - pd.Timedelta(seconds=exec_strength.WINDOW_SEC)) & (s.index < end)]
    return float(w.mean()) if len(w) else None


#: MU 1분봉이 production 스키마(feed=RBAQMU/DNASMU)로 수집된 날만. 10/15·10/16 은 구 로거 포맷.
DAYS = ["20261019", "20261020", "20261021", "20261022", "20261023",
        "20261026", "20261027", "20261028", "20261029", "20261030"]


# ── 1분봉 로딩 ────────────────────────────────────────────────────────────
def load_1m(day: str, tag: str) -> pd.DataFrame | None:
    p = SP / "data" / f"replay_{day}_{tag}_1m.csv"
    if not p.exists():
        return None
    df = pd.read_csv(p)
    df["datetime"] = pd.to_datetime(df["datetime"]).dt.tz_localize(config.KST)
    return df.sort_values("datetime").reset_index(drop=True)


def kst(day: str, hh: int, mm: int) -> datetime:
    d = datetime.strptime(day, "%Y%m%d")
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=config.KST)


# ── MU 플래그 열거 ────────────────────────────────────────────────────────
def mu_flags(series: pd.Series) -> list[tuple[datetime, Direction]]:
    """production 제로크로스 규칙을 MU 1분봉 hist 에 그대로 적용. 반환 = (F_end, 방향)."""
    hist = mu_assist._macd_hist(series)
    out: list[tuple[datetime, Direction]] = []
    last: Direction | None = None
    for i in range(1, len(hist)):
        if i + 1 < int(config.EMA_SLOW):          # production: len(closes) >= EMA_SLOW
            continue
        prev = round(float(hist.iloc[i - 1]), 6)
        cur = round(float(hist.iloc[i]), 6)
        if prev <= 0 and cur > 0:
            pat = Direction.UP_RED
        elif prev >= 0 and cur < 0:
            pat = Direction.DOWN_BLUE
        else:
            continue
        if last == pat:                            # repeat suppression (production 동일)
            continue
        last = pat
        out.append((hist.index[i].to_pydatetime() + timedelta(minutes=1), pat))
    return out


def hy_flags(df1m: pd.DataFrame, day: str) -> list[tuple[datetime, Direction]]:
    """비교용 하이닉스 확정 플래그 (완성 3분봉 제로크로스). 반환 = (확정시각=봉종료, 방향)."""
    out: list[tuple[datetime, Direction]] = []
    last: Direction | None = None
    bars = signal_engine.resample_completed_3m(df1m, kst(day, 15, 40))
    if bars.empty:
        return out
    ser = time_window_filter._gap_series(bars)
    if ser is None:
        return out
    for i in range(1, len(ser)):
        prev, cur = round(float(ser["gap"].iloc[i - 1]), 6), round(float(ser["gap"].iloc[i]), 6)
        if prev <= 0 and cur > 0:
            pat = Direction.UP_RED
        elif prev >= 0 and cur < 0:
            pat = Direction.DOWN_BLUE
        else:
            continue
        if last == pat:
            continue
        last = pat
        out.append((ser["datetime"].iloc[i].to_pydatetime() + timedelta(minutes=3), pat))
    return out


# ── 결과 라벨 ─────────────────────────────────────────────────────────────
def outcome(etf: pd.DataFrame, t: datetime, horizon_min: int = 30) -> dict:
    fwd = etf[etf["datetime"] >= t]
    if fwd.empty:
        return {}
    entry = float(fwd["open"].iloc[0])
    if entry <= 0:
        return {}
    net = lambda px: (px * (1 - FEE) - entry * (1 + FEE)) / entry * 100.0
    win = fwd[fwd["datetime"] < t + timedelta(minutes=horizon_min)]
    tp1, sl = 100.0 * float(config.MORNING_TP1), 100.0 * float(config.X2LITE_MORNING_STOP_LOSS)
    path, path_min = "NEITHER", None
    for _, r in fwd.iterrows():
        hi, lo = net(float(r["high"])), net(float(r["low"]))
        if hi >= tp1 and lo <= sl:
            path, path_min = "BOTH_SAME_BAR", int((r["datetime"].to_pydatetime() - t).total_seconds() // 60)
            break
        if hi >= tp1:
            path, path_min = "TP1", int((r["datetime"].to_pydatetime() - t).total_seconds() // 60)
            break
        if lo <= sl:
            path, path_min = "SL", int((r["datetime"].to_pydatetime() - t).total_seconds() // 60)
            break
    out = {"entry_px": entry, "path": path, "path_min": path_min,
           "mfe30": round(max(net(float(x)) for x in win["high"]), 3),
           "mae30": round(min(net(float(x)) for x in win["low"]), 3)}
    for h in (5, 10, 20, 30):
        seg = fwd[fwd["datetime"] < t + timedelta(minutes=h)]
        out[f"r{h}"] = round(net(float(seg["close"].iloc[-1])), 3) if len(seg) else None
    eod = fwd[fwd["datetime"] <= t.replace(hour=14, minute=50)]
    out["r_eod"] = round(net(float(eod["close"].iloc[-1])), 3) if len(eod) else None
    return out


# ── 하이닉스 보조판정 ─────────────────────────────────────────────────────
def hy_features(df1m: pd.DataFrame, direction: Direction, t: datetime) -> dict:
    sign = 1 if direction == Direction.UP_RED else -1
    f: dict = {"hy_bars": 0}
    bars = signal_engine.resample_completed_3m(df1m, t)
    f["hy_bars"] = len(bars)
    if len(bars) < int(config.EMA_SLOW) + 2:
        return f
    ser = time_window_filter._gap_series(bars)
    if ser is None or len(ser) < 4:
        return f
    g_now = float(ser["gap"].iloc[-1]) * sign
    g_prev = float(ser["gap"].iloc[-2]) * sign
    f["hy_gap_same"] = round(g_now, 3)
    f["hy_gap_prev"] = round(g_prev, 3)
    f["hy_gap_opp"] = round(max(0.0, -g_now), 3)          # 반대방향 gap 크기
    f["hy_gap_expand"] = bool(g_now > g_prev)
    f["hy_aligned"] = bool(g_now > 0)

    # TEG — MU 판정 시각의 마지막 두 3분봉을 (flag, confirm) 으로 본다
    try:
        teg = teg_gate.evaluate_teg(bars, direction,
                                    ser["datetime"].iloc[-2].to_pydatetime(), t)
        f["teg_ok"] = bool(teg.approved)
        f["teg_fail"] = "" if teg.approved else ",".join(
            k for k, v in (teg.conditions or {}).items() if not v)[:60]
    except Exception as exc:
        f["teg_ok"], f["teg_fail"] = None, f"ERR:{type(exc).__name__}"

    # major_flag_filter — 점수 / breakout / VWAP
    try:
        tmpl, metrics, err = major_flag_filter.compute_component_scores(bars)
        if tmpl is not None:
            sc, m = major_flag_filter.score_for_direction(tmpl, metrics, direction)
            f["hy_score"] = round(float(sum(sc.values())), 1)
            f["hy_breakout"] = bool(m.get("breakout"))
            vwap = float(m.get("vwap")) if m.get("vwap") is not None else None
            close = float(m.get("close"))
            if vwap:
                f["hy_vwap_ok"] = bool((close >= vwap) if sign > 0 else (close <= vwap))
                f["hy_vwap_dist"] = round(sign * (close - vwap) / vwap * 100.0, 3)
        else:
            f["hy_score_err"] = str(err)[:40]
    except Exception as exc:
        f["hy_score_err"] = f"ERR:{type(exc).__name__}"

    # 최근 30분 확정교차 수 (TW2 veto / TEG 조건2 가 쓰는 바로 그 계산기)
    try:
        f["hy_cross30"] = int(time_window_filter._count_recent_confirmed_crossovers(
            bars, t, 30))
    except Exception:
        pass

    # chop_defense 재료 — EMA 추세 / 경로 chop
    try:
        work = chop_defense._completed_intraday(df1m, t)
        if work is not None and len(work):
            f["cd_trend_ok"] = bool(chop_defense._trend_ok(work, sign > 0))
            f["cd_path_chop"] = bool(chop_defense._path_is_chop(work))
    except Exception:
        pass

    # 추세지속 필터 (1분봉은 판정 시각까지만 — 미래 차단)
    try:
        past1m = df1m[df1m["datetime"] <= t]
        tp = trend_persistence_filter.evaluate_trend_persistence(bars, past1m, direction, t)
        f["tp_decision"] = str(getattr(tp, "decision", ""))[:28]
        f["tp_score"] = round(float(getattr(tp, "score", float("nan"))), 1)
    except Exception as exc:
        f["tp_decision"] = f"ERR:{type(exc).__name__}"
    return f


# ── main ──────────────────────────────────────────────────────────────────
def main() -> None:
    rows = []
    for day in DAYS:
        hy1m = load_1m(day, "hynix")
        long1m, inv1m = load_1m(day, "long"), load_1m(day, "inverse")
        if hy1m is None or long1m is None or inv1m is None:
            print(f"{day}: 1분봉 없음 — 건너뜀")
            continue
        mu = mu_assist.load_mu_1m(kst(day, 12, 0), str(RT))
        if mu is None or mu.empty:
            print(f"{day}: MU 분봉 없음 — 건너뜀")
            continue
        flags = mu_flags(mu)
        hyf = hy_flags(hy1m, day)
        print(f"{day}: MU봉 {len(mu)}  MU플래그 {len(flags)}  HY플래그 {len(hyf)}")
        for f_end, d in flags:
            t = f_end + timedelta(minutes=2)
            tk = t.replace(tzinfo=config.KST)
            row = {"day": day, "flag_end": f_end.strftime("%H:%M"), "decide": t.strftime("%H:%M"),
                   "dir": "RED" if d == Direction.UP_RED else "BLUE"}
            # MU 패턴 — flag_bar_dt 를 F_end-3분으로 주어 d0 = F_end 가 되게 한다
            pat = mu_assist.evaluate_pattern(mu, d, f_end - timedelta(minutes=3))
            row.update({"mu_state": pat["mu_pattern_state"], "mu_post2": pat["mu_post2_bp"],
                        "mu_acc2": pat["mu_acc2"], "mu_t2": pat["mu_t2_status"]})
            # 체결강도
            c = mean_cttr_full(tk)
            row["cttr"] = round(c, 2) if c is not None else None
            row["xs"] = exec_strength.state_for(d, c)
            # 거래시간대
            row["window"] = time_window_filter.classify_window(t.time()) or ""
            row["tradable"] = bool(config.SESSION_OPEN <= t.time() <= datetime(2000, 1, 1, 14, 50).time())
            # 하이닉스 보조
            row.update(hy_features(hy1m, d, tk))
            # HY 플래그 대비 선행분
            fk = f_end.replace(tzinfo=config.KST)
            nxt = sorted(h for h, hd in hyf if hd == d and timedelta(0) <= h - fk <= timedelta(hours=1))
            row["hy_same_flag_lead_min"] = int((nxt[0] - fk).total_seconds() // 60) if nxt else None
            # 결과
            etf = long1m if d == Direction.UP_RED else inv1m
            row.update(outcome(etf, tk))
            rows.append(row)

    df = pd.DataFrame(rows)
    OUT.mkdir(parents=True, exist_ok=True)
    df.to_csv(OUT / "mu_flags.csv", index=False, encoding="utf-8-sig")
    print(f"\n총 MU 플래그 {len(df)}건 -> {OUT / 'mu_flags.csv'}")
    if len(df):
        tr = df[df["tradable"] & df["entry_px"].notna()]
        print(f"거래가능 시간대(09:00~14:50) {len(tr)}건")
        print(tr.groupby(["mu_state"])["r30"].agg(["count", "mean"]).round(2).to_string())


if __name__ == "__main__":
    main()
