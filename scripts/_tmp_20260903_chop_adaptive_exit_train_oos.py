"""READ-ONLY backtest (2026-09-03 사용자 요청): TW2 3-SLOT 진입 로직은 전혀
건드리지 않고 **청산만** market regime(CHOP / TREND)에 따라 다르게 하는 세 가지
정책 비교.

  A. 현행 청산 — TP1(3.0%)/TP2(TW2_MORNING_TP2=6.0%)/trailing/손절 그대로
  B. CHOP adaptive exit — CHOP이면 순수익 +1.5% 도달 시 전량익절,
     TREND이면 A와 동일
  C. B와 동일하되 CHOP 전량익절 임계값 +2.0%

절대 production을 수정/커밋/푸시하지 않는다. 진입/청산에 쓰이는 모든 판단은
production 함수를 그대로 재사용한다(신규 구현 없음):
  - MACD 크로스오버:  signal_engine.calculate_macd / evaluate_macd_crossover
  - T+3 기본 게이트:  time_window_filter.evaluate_time_window_entry
  - TW2 추가 veto:    time_window_filter.evaluate_tw2_extra_vetoes
  - 슬롯/세션/일한도:  time_window_3slot.resolve_slot
  - Trend Quality:    time_window_3slot.evaluate_trend_quality
  - TEGv2:            teg_gate.evaluate_teg
  - 휩쏘 분류/추적:    config.TW_WHIPSAW_REJECT_REASONS +
                      time_window_filter.evaluate_whipsaw_watch
  - 청산 래더:         time_window_position_manager.
                      evaluate_take_profit_immediate / evaluate_position
                      (production과 동일한 TW2_MORNING_TP2 override)
  - 비용/순수익:       worker._net_return_pct (TradeCostEngine)
  - 방향→종목:         order_executor.target_symbol_for_direction
  - CHOP 판정 재료:    time_window_filter._gap_series /
                      _confirmed_flag_indices (확정 zero-cross 검출),
                      major_flag_filter._session_vwap (VWAP),
                      config.MAJOR_EMA_FAST/SLOW (EMA10/EMA20)

새로 쓴 코드는 (1) 3분봉 오케스트레이션 루프(worker._resolve_tw2_3slot_
candidate_body의 제어흐름을 그대로 미러링, scripts/_tmp_20260903_adaptive_
2plus1_30day_backtest.py에서 이미 검증된 것과 동일 구조)와 (2) 검증 대상인
CHOP 판정식 + CHOP 전량익절 규칙뿐이다.

두 가지 중요한 방법론 결정
--------------------------
1) **진입 동결(frozen entries).** "진입 로직은 전혀 변경하지 말라"는 요구를
   글자 그대로 지키기 위해, 먼저 A를 production 제어흐름 그대로 돌려 매
   후보(pending)의 승인/거절 결과를 기록하고, B/C는 그 결과를 그대로 재생한다.
   그냥 B/C에서 게이트를 다시 계산하면 청산이 빨라져 포지션이 먼저 비게 되고,
   evaluate_time_window_entry의 `position_direction == direction` 피라미딩
   금지 분기(time_window_filter.py:860)와 resolve_slot의 `is_flat` 분기가
   **A에는 없던 새 진입을 만들어낸다** — 즉 청산 변경이 진입을 바꿔버린다.
   진입을 동결하면 A/B/C의 진입 집합이 완전히 동일해져 사용자가 요청한
   pair comparison이 정확히 성립한다. (참고용으로 진입을 동결하지 않은
   free-chain 변형도 함께 돌려 부작용 크기를 같이 보고한다.)
2) **TP1 부분매도를 실제로 반영.** 기존 비교 백테스트들은 qty=1 관례상 TP1
   50% 부분매도를 무시하고 최종 청산가 기준 전량 수익률만 기록했다. 이번
   비교는 정확히 "TP 경로"를 바꾸는 것이므로 그 근사가 A에 불리/유리하게
   편향될 수 있다. 따라서 잔량 비중(qty_frac)을 추적해
   net_pct = Σ(레그 비중 × 레그 순수익률)로 계산한다. 기존 관례값도
   net_pct_fullqty로 함께 기록해 이전 리포트와 비교 가능하게 남긴다.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any, Optional

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))
if str(PROJECT_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "scripts"))

from app.trading.macd2 import config, order_executor, teg_gate  # noqa: E402
from app.trading.macd2 import time_window_3slot as tw3  # noqa: E402
from app.trading.macd2 import time_window_filter as twf  # noqa: E402
from app.trading.macd2 import time_window_position_manager as twpm  # noqa: E402
from app.trading.macd2.major_flag_filter import _session_vwap  # noqa: E402
from app.trading.macd2.models import Direction  # noqa: E402
from app.trading.macd2.signal_engine import calculate_macd, evaluate_macd_crossover, resample_completed_3m  # noqa: E402
from app.trading.macd2.worker import _net_return_pct  # noqa: E402

import backtest_time_window_filter as bt  # noqa: E402

KST = config.KST
CACHE_DIR = PROJECT_ROOT / "data" / "cache"
OUTPUT_DIR = PROJECT_ROOT / "data" / "validation" / "chop_adaptive_exit"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

N_DAYS = 30
N_TRAIN = 20
CHOP_LOOKBACK_BARS = 10       # 10 completed 3m bars == 최근 30분
CHOP_MIN_BARS = 4             # 30분 창에 최소 4봉은 있어야 판정 (없으면 TREND)
EXIT_CHOP_FULL_TP = "CHOP_ADAPTIVE_FULL_TP"


# ── data loading ─────────────────────────────────────────────────────────
def _common_dates() -> list[str]:
    def dates_for(tag: str) -> set[str]:
        return {
            p.stem.split("_")[1] for p in CACHE_DIR.glob(f"replay_*_{tag}_1m.csv")
            if p.stem.split("_")[1].isdigit() and len(p.stem.split("_")[1]) == 8
        }
    return sorted(dates_for("hynix") & dates_for("long") & dates_for("inverse"))


def _load_1m(path: Path) -> pd.DataFrame:
    df = pd.read_csv(path)
    df["datetime"] = pd.to_datetime(df["datetime"])
    if df["datetime"].dt.tz is None:
        df["datetime"] = df["datetime"].dt.tz_localize(KST)
    return df[["datetime", "open", "high", "low", "close", "volume"]]


def _load_all(tag: str, dates: list[str]) -> pd.DataFrame:
    frames = [_load_1m(CACHE_DIR / f"replay_{d}_{tag}_1m.csv") for d in dates]
    return (
        pd.concat(frames, ignore_index=True)
        .drop_duplicates("datetime", keep="last")
        .sort_values("datetime")
        .reset_index(drop=True)
    )


# ── CHOP feature precomputation (production indicator functions only) ────
@dataclass
class ChopFeatures:
    """봉별(방향 무관) 원시값. 방향 부호는 사용 시점에 곱한다."""
    valid: list          # 30분 창 판정 가능 여부
    cross30: list        # 최근 30분 확정 zero-cross(=confirmed crossover) 횟수
    spread_chg: list     # (EMA10-EMA20)[i] - (EMA10-EMA20)[anchor]  (미부호)
    ema20_chg: list      # EMA20[i] - EMA20[anchor]                  (미부호)
    vwap_flip: list      # 최근 30분 close-VWAP 부호 반복(교차) 횟수


def build_chop_features(bars_3m: pd.DataFrame) -> ChopFeatures:
    """모든 재료를 production 함수로 계산한다. 전체 프레임 한 번에 계산해도
    prefix 계산과 값이 동일하다 — EMA는 ewm(adjust=False) 재귀식(인과적)이고
    _session_vwap은 일자별 cumsum(인과적)이며 _confirmed_flag_indices는
    gap 시계열을 앞에서부터 걷는 인과적 워크이기 때문. (main()에서 표본
    봉들에 대해 production _count_recent_confirmed_crossovers 원함수 호출과
    일치하는지 실제로 assert 한다.)"""
    n = len(bars_3m)
    close = bars_3m["close"].astype(float).reset_index(drop=True)
    ema10 = close.ewm(span=config.MAJOR_EMA_FAST, adjust=False).mean()
    ema20 = close.ewm(span=config.MAJOR_EMA_SLOW, adjust=False).mean()
    spread = (ema10 - ema20).tolist()
    ema20_l = ema20.tolist()

    vwap = _session_vwap(bars_3m).reset_index(drop=True)
    kst_dt = bars_3m["datetime"].dt.tz_convert(KST)
    day = kst_dt.dt.strftime("%Y%m%d").tolist()
    tod = [t.time() for t in kst_dt]
    in_session = [tod[i] >= config.SESSION_OPEN for i in range(n)]

    # 각 봉이 속한 "당일 정규장 첫 봉" 인덱스 (30분 창이 전일/장전으로 새는 것 방지)
    first_in_session: list = [0] * n
    cur_day = None
    cur_first = 0
    for i in range(n):
        if day[i] != cur_day:
            cur_day = day[i]
            cur_first = None
        if cur_first is None and in_session[i]:
            cur_first = i
        first_in_session[i] = cur_first if cur_first is not None else i

    # 확정 zero-cross: production 검출기를 그대로 사용
    series = twf._gap_series(bars_3m)
    flag_confirm_times: list = []
    if series is not None:
        for i, _d in twf._confirmed_flag_indices(series):
            bar_dt = pd.Timestamp(series["datetime"].iloc[i]).to_pydatetime()
            if bar_dt.astimezone(KST).time() < config.SESSION_OPEN:
                continue  # production _count_recent_confirmed_crossovers와 동일한 장전 제외
            flag_confirm_times.append(bar_dt + timedelta(minutes=3))

    valid: list = [False] * n
    cross30: list = [0] * n
    spread_chg: list = [0.0] * n
    ema20_chg: list = [0.0] * n
    vwap_flip: list = [0] * n

    bar_dts = [pd.Timestamp(x).to_pydatetime() for x in bars_3m["datetime"]]
    for i in range(n):
        if not in_session[i]:
            continue
        anchor = max(i - CHOP_LOOKBACK_BARS, first_in_session[i])
        if i - anchor < CHOP_MIN_BARS:
            continue
        valid[i] = True
        spread_chg[i] = spread[i] - spread[anchor]
        ema20_chg[i] = ema20_l[i] - ema20_l[anchor]

        decision_at = bar_dts[i] + timedelta(minutes=3)
        window_start = decision_at - timedelta(minutes=CHOP_LOOKBACK_BARS * 3)
        cross30[i] = sum(1 for ct in flag_confirm_times if window_start <= ct < decision_at)

        flips = 0
        prev_sign = 0
        for j in range(anchor, i + 1):
            v = float(vwap.iloc[j])
            if not pd.notna(v) or v <= 0:
                continue
            s = 1 if close.iloc[j] > v else (-1 if close.iloc[j] < v else 0)
            if s == 0:
                continue
            if prev_sign != 0 and s != prev_sign:
                flips += 1
            prev_sign = s
        vwap_flip[i] = flips

    return ChopFeatures(valid=valid, cross30=cross30, spread_chg=spread_chg,
                        ema20_chg=ema20_chg, vwap_flip=vwap_flip)


@dataclass(frozen=True)
class ChopConfig:
    cross_min: int
    flip_min: int
    score_min: int

    def key(self) -> str:
        return f"cross>={self.cross_min}|flip>={self.flip_min}|score>={self.score_min}"


def chop_conditions(feat: ChopFeatures, idx: int, direction: Direction, cfg: ChopConfig) -> Optional[dict]:
    """idx 봉 종가 시점(=idx봉 완성 시각)에 알 수 있는 정보만 사용."""
    if idx < 0 or idx >= len(feat.valid) or not feat.valid[idx]:
        return None
    sign = 1 if direction == Direction.UP_RED else -1
    c1 = feat.cross30[idx] >= cfg.cross_min                    # 30분 확정 교차 과다
    c2 = (feat.spread_chg[idx] * sign) <= 0                    # EMA10/EMA20 spread 확대 실패
    c3 = (feat.ema20_chg[idx] * sign) <= 0                     # EMA20 slope 진입방향 아님
    c4 = feat.vwap_flip[idx] >= cfg.flip_min                   # VWAP 상하 반복 과다
    score = int(c1) + int(c2) + int(c3) + int(c4)
    return {
        "cross_over": bool(c1), "spread_not_expanding": bool(c2),
        "ema20_slope_not_aligned": bool(c3), "vwap_repeat": bool(c4),
        "score": score, "is_chop": score >= cfg.score_min,
        "cross30": feat.cross30[idx], "vwap_flip30": feat.vwap_flip[idx],
        "spread_chg_signed": feat.spread_chg[idx] * sign,
        "ema20_chg_signed": feat.ema20_chg[idx] * sign,
    }


# ── records ──────────────────────────────────────────────────────────────
@dataclass
class TradeRec:
    date: str
    slot_number: Optional[int]
    session: Optional[str]
    direction: str
    entry_time: Optional[str] = None
    entry_symbol: Optional[str] = None
    entry_price: Optional[float] = None
    entry_bar_idx: Optional[int] = None
    exit_time: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    exit_bar_idx: Optional[int] = None
    net_pct: Optional[float] = None          # TP1 부분매도 반영 (가중합)
    net_pct_fullqty: Optional[float] = None  # 기존 관례(최종 청산가 전량)
    tp1_hit: bool = False
    tp2_hit: bool = False
    peak_net_pct: float = 0.0
    trough_net_pct: float = 0.0
    chop_entry: Optional[bool] = None        # 진입 당시 CHOP
    chop_latch_bar_idx: Optional[int] = None # CHOP 확정된 봉 (None = TREND 유지)
    chop_latch_time: Optional[str] = None
    chop_latch_after_tp1: bool = False
    chop_score_entry: Optional[int] = None
    hold_bars: int = 0


def _fmt(ts) -> str:
    return pd.Timestamp(ts).isoformat()


# ── the single orchestration loop (A / B / C share it) ───────────────────
def run(
    *,
    hynix_bars_3m: pd.DataFrame,
    flags_by_idx: dict,
    etf_close: dict,
    etf_1m_close: dict,
    dates: list[str],
    feat: ChopFeatures,
    chop_cfg: Optional[ChopConfig],
    chop_tp_pct: Optional[float],
    frozen: Optional[dict] = None,
    record_decisions: bool = False,
) -> tuple[list, dict]:
    """`frozen`이 주어지면 각 후보의 승인/거절 결과를 그대로 재생한다
    (진입 동결). `chop_tp_pct`가 None이면 정책 A(현행 청산)."""
    trades: list = []
    decisions: dict = {}
    position: Optional[dict] = None
    pending = None
    date_set = set(dates)

    slots_used_today = 0
    morning_count = 0
    afternoon_count = 0
    last_afternoon_direction: Optional[str] = None
    current_day: Optional[str] = None
    whipsaw_watch: Optional[dict] = None

    def position_direction():
        return bt._direction_for_symbol(position["symbol"]) if position is not None else None

    def net_at(price: float) -> float:
        return float(_net_return_pct(position["symbol"], position["rec"].entry_price, price, 1))

    def close_trade(exit_time, exit_price, reason, idx):
        rec = position["rec"]
        rec.exit_time = _fmt(exit_time)
        rec.exit_price = exit_price
        rec.exit_reason = reason
        rec.exit_bar_idx = idx
        leg = net_at(exit_price)
        rec.net_pct = round(position["realized"] + position["qty_frac"] * leg, 6)
        rec.net_pct_fullqty = round(leg, 6)
        rec.hold_bars = int(idx - rec.entry_bar_idx)
        trades.append(rec)

    def maybe_latch_chop(idx: int) -> None:
        """idx 봉 완성 시점 정보로 CHOP 판정 (한 번 CHOP이면 그 포지션 동안 유지)."""
        if position is None or chop_cfg is None or position["chop_latched"]:
            return
        d = bt._direction_for_symbol(position["symbol"])
        if d is None:
            return
        c = chop_conditions(feat, idx, d, chop_cfg)
        if c is not None and c["is_chop"]:
            position["chop_latched"] = True
            rec = position["rec"]
            rec.chop_latch_bar_idx = idx
            rec.chop_latch_time = _fmt(pd.Timestamp(hynix_bars_3m["datetime"].iloc[idx]) + timedelta(minutes=3))
            rec.chop_latch_after_tp1 = bool(position["tp1_done"])

    for idx in range(len(hynix_bars_3m)):
        bar_ts = hynix_bars_3m["datetime"].iloc[idx]
        bar_start = pd.Timestamp(bar_ts).to_pydatetime()
        day_key = bar_start.strftime("%Y%m%d")
        if day_key not in date_set:
            continue
        if day_key != current_day:
            current_day = day_key
            slots_used_today = 0
            morning_count = 0
            afternoon_count = 0
            last_afternoon_direction = None
            pending = None
            whipsaw_watch = None
        bar_close_at = bar_start + timedelta(minutes=3)

        # 1) 15:00 강제청산
        if position is not None and bar_close_at.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                close_trade(bar_close_at, close, config.EXIT_FORCED_LIQUIDATION, idx)
                position = None
            pending = None
            whipsaw_watch = None

        # 2) 직전 봉에 등록된 후보를 이 봉(T+3)에서 확정 판정
        if pending is not None:
            p_direction, p_idx, p_bar_ts = pending
            if idx == p_idx + 1:
                pending = None
                flag_bar_dt = pd.Timestamp(p_bar_ts).to_pydatetime()
                bars_slice = hynix_bars_3m.iloc[: idx + 1]
                dec_key = f"{idx}|{p_direction.value}"

                if frozen is not None:
                    fr = frozen.get(dec_key)
                    if fr is None:
                        final_approved, final_reason = False, "FROZEN_NO_DECISION"
                        slot_number = session = None
                    else:
                        final_approved = fr["approved"]
                        final_reason = fr["reason"]
                        slot_number = fr["slot_number"]
                        session = fr["session"]
                else:
                    slot_number = None
                    session = None
                    base_decision = twf.evaluate_time_window_entry(
                        bars_slice, p_direction, flag_bar_dt, bar_close_at,
                        position_direction=position_direction(),
                        morning_entry_count=0, afternoon_entry_count=0, daily_entry_count=0,
                    )
                    tw2_cleared = bool(base_decision.approved)
                    base_reason = base_decision.block_reason
                    if tw2_cleared:
                        vetoed, veto_reason = twf.evaluate_tw2_extra_vetoes(
                            bars_slice, p_direction, flag_bar_dt, bar_close_at)
                        if vetoed:
                            tw2_cleared = False
                            base_reason = veto_reason
                    final_approved = False
                    final_reason = base_reason
                    if tw2_cleared:
                        slot_decision = tw3.resolve_slot(
                            now=bar_close_at, slots_used_today=slots_used_today,
                            morning_count=morning_count, afternoon_count=afternoon_count,
                            direction=p_direction, is_flat=(position is None),
                            last_afternoon_direction=last_afternoon_direction,
                        )
                        slot_number = slot_decision.slot_number
                        session = slot_decision.session
                        if not slot_decision.slot_allowed:
                            final_reason = slot_decision.reject_reason
                        elif slot_decision.requires_quality_gate:
                            q = tw3.evaluate_trend_quality(bars_slice, p_direction)
                            final_approved = q.approved
                            final_reason = config.TW_APPROVED if q.approved else config.TW2_3SLOT_REJECT_QUALITY
                        elif slot_decision.requires_teg_gate:
                            t = teg_gate.evaluate_teg(bars_slice, p_direction, flag_bar_dt, bar_close_at)
                            final_approved = t.approved
                            final_reason = config.TW_APPROVED if t.approved else config.TW2_3SLOT_REJECT_TEG
                        else:
                            final_approved = True
                            final_reason = config.TW_APPROVED
                    if record_decisions:
                        decisions[dec_key] = {
                            "approved": bool(final_approved), "reason": str(final_reason),
                            "slot_number": slot_number, "session": session,
                            "date": current_day, "direction": p_direction.value,
                        }

                target = order_executor.target_symbol_for_direction(p_direction)
                if not final_approved:
                    is_whipsaw = final_reason in config.TW_WHIPSAW_REJECT_REASONS
                    if position is not None and position["symbol"] != target:
                        if is_whipsaw:
                            whipsaw_watch = {"direction": p_direction,
                                             "last_gap": float("-inf"), "last_ema_spread": float("-inf")}
                        else:
                            close_now = etf_close[position["symbol"]].get(bar_ts, position["rec"].entry_price)
                            close_trade(bar_close_at, close_now, config.EXIT_OPPOSITE_SIGNAL, idx)
                            position = None
                else:
                    fill = etf_close[target].get(bar_ts)
                    if fill is not None:
                        if position is not None and position["symbol"] != target:
                            close_now = etf_close[position["symbol"]].get(bar_ts, position["rec"].entry_price)
                            close_trade(bar_close_at, close_now, config.EXIT_OPPOSITE_SIGNAL, idx)
                            position = None
                        if position is None:
                            slots_used_today += 1
                            if session == tw3.SESSION_MORNING:
                                morning_count += 1
                            else:
                                afternoon_count += 1
                                last_afternoon_direction = p_direction.value
                            rec = TradeRec(
                                date=current_day, slot_number=slot_number, session=session,
                                direction=p_direction.value, entry_time=bar_close_at.isoformat(),
                                entry_symbol=target, entry_price=fill, entry_bar_idx=idx,
                            )
                            position = {"symbol": target, "entry_idx": idx, "entry_time": bar_close_at,
                                        "tp1_done": False, "peak": 0.0, "session": session, "rec": rec,
                                        "qty_frac": 1.0, "realized": 0.0, "chop_latched": False}
                            whipsaw_watch = None
                            # 진입 당시 CHOP 판정 (확정봉 = idx, 진입시각 = 그 봉 종료시각)
                            if chop_cfg is not None:
                                c = chop_conditions(feat, idx, p_direction, chop_cfg)
                                rec.chop_entry = bool(c["is_chop"]) if c is not None else False
                                rec.chop_score_entry = c["score"] if c is not None else None
                                maybe_latch_chop(idx)

        # 3) 이 봉에서 새 플래그 등록
        if idx in flags_by_idx:
            flag_time = bar_start.astimezone(KST).time()
            if config.SESSION_OPEN <= flag_time < config.NEW_ENTRY_CUTOFF:
                pending = (flags_by_idx[idx], idx, bar_ts)

        # 4) 휩쏘 추적
        if whipsaw_watch is not None and position is not None:
            decision = twf.evaluate_whipsaw_watch(
                hynix_bars_3m.iloc[: idx + 1], whipsaw_watch["direction"],
                whipsaw_watch["last_gap"], whipsaw_watch["last_ema_spread"],
            )
            if not decision.insufficient_data:
                if decision.should_release:
                    whipsaw_watch = None
                elif decision.should_sell:
                    close = etf_close[position["symbol"]].get(bar_ts)
                    if close is not None:
                        close_trade(bar_close_at, close, "WHIPSAW_WATCH_DETERIORATION_EXIT", idx)
                        position = None
                    whipsaw_watch = None
                else:
                    whipsaw_watch["last_gap"] = decision.current_gap
                    whipsaw_watch["last_ema_spread"] = decision.current_ema_spread

        # 5) 틱(1분 종가) 익절 체크 — CHOP 상태는 "직전 완성봉"까지의 정보만 사용
        if position is not None:
            for minute_offset in range(3):
                tick_time = bar_start + timedelta(minutes=minute_offset)
                if tick_time <= position["entry_time"] or tick_time > bar_close_at:
                    continue
                price = etf_1m_close[position["symbol"]].get(pd.Timestamp(tick_time))
                if price is None:
                    continue
                net = net_at(price)
                position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                position["rec"].trough_net_pct = min(position["rec"].trough_net_pct, net)
                if chop_tp_pct is not None and position["chop_latched"] and net >= chop_tp_pct:
                    close_trade(tick_time, price, EXIT_CHOP_FULL_TP, idx)
                    position = None
                    whipsaw_watch = None
                    break
                tp = twpm.evaluate_take_profit_immediate(
                    session=position["session"], net_return_pct=net, tp1_done=position["tp1_done"],
                    tp2_pct_override=config.TW2_MORNING_TP2 * 100.0,
                )
                position["peak"] = max(position["peak"], tp.peak_net_return)
                if tp.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                    position["rec"].tp1_hit = True
                    position["realized"] += position["qty_frac"] * tp.sell_fraction * net
                    position["qty_frac"] *= (1.0 - tp.sell_fraction)
                    position["tp1_done"] = tp.tp1_done
                elif tp.exit_reason is not None:
                    if tp.exit_reason == config.EXIT_TW_TP2_FULL:
                        position["rec"].tp2_hit = True
                    close_trade(tick_time, price, tp.exit_reason, idx)
                    position = None
                    whipsaw_watch = None
                    break
                else:
                    position["tp1_done"] = tp.tp1_done

        # 6) 완성봉 기준 CHOP 재판정 + 청산 래더
        if position is not None:
            maybe_latch_chop(idx)
        if position is not None and idx > position["entry_idx"]:
            close = etf_close[position["symbol"]].get(bar_ts)
            if close is not None:
                net = net_at(close)
                position["rec"].peak_net_pct = max(position["rec"].peak_net_pct, net)
                position["rec"].trough_net_pct = min(position["rec"].trough_net_pct, net)
                if chop_tp_pct is not None and position["chop_latched"] and net >= chop_tp_pct:
                    close_trade(bar_close_at, close, EXIT_CHOP_FULL_TP, idx)
                    position = None
                    whipsaw_watch = None
                else:
                    pm = twpm.evaluate_position(
                        session=position["session"], net_return_pct=net,
                        tp1_done=position["tp1_done"], peak_net_return=position["peak"],
                        tp2_pct_override=config.TW2_MORNING_TP2 * 100.0,
                    )
                    position["peak"] = pm.peak_net_return
                    if pm.exit_reason == config.EXIT_TW_TP1_PARTIAL:
                        position["rec"].tp1_hit = True
                        position["realized"] += position["qty_frac"] * pm.sell_fraction * net
                        position["qty_frac"] *= (1.0 - pm.sell_fraction)
                        position["tp1_done"] = pm.tp1_done
                    elif pm.exit_reason is not None:
                        if pm.exit_reason == config.EXIT_TW_TP2_FULL:
                            position["rec"].tp2_hit = True
                        close_trade(bar_close_at, close, pm.exit_reason, idx)
                        position = None
                        whipsaw_watch = None
                    else:
                        position["tp1_done"] = pm.tp1_done

    if position is not None:
        last_idx = len(hynix_bars_3m) - 1
        last_dt = pd.Timestamp(hynix_bars_3m["datetime"].iloc[last_idx]).to_pydatetime() + timedelta(minutes=3)
        close = etf_close[position["symbol"]].get(hynix_bars_3m["datetime"].iloc[last_idx],
                                                  position["rec"].entry_price)
        close_trade(last_dt, close, "END_OF_DATA", last_idx)

    return trades, decisions


# ── shared setup (cached so later stages don't recompute the MACD walk) ──
@dataclass
class Ctx:
    dates: list
    train_dates: list
    oos_dates: list
    warmup: str
    hynix_bars_3m: pd.DataFrame
    flags_by_idx: dict
    etf_close: dict
    etf_1m_close: dict
    feat: ChopFeatures


_CTX_CACHE = OUTPUT_DIR / "_ctx_cache.pkl"


def build_ctx(use_cache: bool = True) -> Ctx:
    import pickle

    if use_cache and _CTX_CACHE.exists():
        with open(_CTX_CACHE, "rb") as fh:
            raw = pickle.load(fh)
        raw["feat"] = ChopFeatures(**raw["feat"])
        return Ctx(**raw)

    all_dates = [d for d in _common_dates() if d != datetime.now(KST).strftime("%Y%m%d")]
    dates = all_dates[-N_DAYS:]
    warmup = all_dates[all_dates.index(dates[0]) - 1]

    hynix_all = _load_all("hynix", [warmup] + dates)
    long_all = _load_all("long", dates)
    inverse_all = _load_all("inverse", dates)
    end = datetime.combine(pd.Timestamp(dates[-1]).date(), dtime(20, 0), tzinfo=KST)
    hynix_bars_3m = resample_completed_3m(hynix_all, now=end)
    long_bars_3m = resample_completed_3m(long_all, now=end)
    inverse_bars_3m = resample_completed_3m(inverse_all, now=end)

    etf_close = {
        config.LONG_SYMBOL: bt._etf_close_lookup(long_bars_3m),
        config.INVERSE_SYMBOL: bt._etf_close_lookup(inverse_bars_3m),
    }
    etf_1m_close = {
        config.LONG_SYMBOL: dict(zip(long_all["datetime"], long_all["close"])),
        config.INVERSE_SYMBOL: dict(zip(inverse_all["datetime"], inverse_all["close"])),
    }

    # 확정 플래그 (production 크로스오버 판정, 일자 경계에서 prev_direction 리셋)
    flags_by_idx: dict = {}
    prev_direction = None
    last_bar_date = None
    date_set = set(dates)
    for i in range(len(hynix_bars_3m)):
        snap = calculate_macd(hynix_bars_3m.iloc[: i + 1])
        if snap is None:
            continue
        bar_date = pd.Timestamp(hynix_bars_3m["datetime"].iloc[i]).astimezone(KST).strftime("%Y%m%d")
        if last_bar_date is None or bar_date != last_bar_date:
            prev_direction = None
        last_bar_date = bar_date
        direction = evaluate_macd_crossover(snap, prev_direction)
        if direction in (Direction.UP_RED, Direction.DOWN_BLUE):
            prev_direction = direction
            if bar_date in date_set:
                flags_by_idx[i] = direction

    ctx = Ctx(
        dates=dates, train_dates=dates[:N_TRAIN], oos_dates=dates[N_TRAIN:], warmup=warmup,
        hynix_bars_3m=hynix_bars_3m, flags_by_idx=flags_by_idx,
        etf_close=etf_close, etf_1m_close=etf_1m_close,
        feat=build_chop_features(hynix_bars_3m),
    )
    payload = dict(vars(ctx))
    payload["feat"] = dict(vars(ctx.feat))
    with open(_CTX_CACHE, "wb") as fh:
        pickle.dump(payload, fh)
    return ctx


def verify_cross30(ctx: Ctx) -> tuple[int, int]:
    """precomputed cross30가 production 원함수와 일치하는지 표본 검증."""
    mismatches = checked = 0
    step = max(1, len(ctx.hynix_bars_3m) // 40)
    for i in range(0, len(ctx.hynix_bars_3m), step):
        if not ctx.feat.valid[i]:
            continue
        decision_at = pd.Timestamp(ctx.hynix_bars_3m["datetime"].iloc[i]).to_pydatetime() + timedelta(minutes=3)
        ref = twf._count_recent_confirmed_crossovers(
            ctx.hynix_bars_3m.iloc[: i + 1], decision_at, CHOP_LOOKBACK_BARS * 3)
        checked += 1
        if ref != ctx.feat.cross30[i]:
            mismatches += 1
    return checked, mismatches


def run_policy(ctx: Ctx, *, chop_cfg, chop_tp_pct, frozen, record=False):
    return run(
        hynix_bars_3m=ctx.hynix_bars_3m, flags_by_idx=ctx.flags_by_idx,
        etf_close=ctx.etf_close, etf_1m_close=ctx.etf_1m_close, dates=ctx.dates,
        feat=ctx.feat, chop_cfg=chop_cfg, chop_tp_pct=chop_tp_pct,
        frozen=frozen, record_decisions=record,
    )


def stage_prep() -> int:
    ctx = build_ctx(use_cache=False)
    print(f"전체 {len(ctx.dates)}영업일: {ctx.dates[0]} ~ {ctx.dates[-1]} (워밍업 {ctx.warmup})")
    print(f"  TRAIN({len(ctx.train_dates)}): {ctx.train_dates[0]} ~ {ctx.train_dates[-1]}")
    print(f"  OOS  ({len(ctx.oos_dates)}): {ctx.oos_dates[0]} ~ {ctx.oos_dates[-1]}")
    print(f"3분봉 {len(ctx.hynix_bars_3m)}개, 확정 플래그 {len(ctx.flags_by_idx)}개")
    checked, mism = verify_cross30(ctx)
    print(f"cross30 검증: 표본 {checked}개, production 원함수와 불일치 {mism}개")
    assert mism == 0, "precomputed cross30 diverges from production _count_recent_confirmed_crossovers"

    trades_a, decisions = run_policy(ctx, chop_cfg=None, chop_tp_pct=None, frozen=None, record=True)
    approved = sum(1 for d in decisions.values() if d["approved"])
    print(f"[A] 거래 {len(trades_a)}건 / 후보판정 {len(decisions)}건 (승인 {approved}건)")
    (OUTPUT_DIR / "baseline_A.json").write_text(json.dumps({
        "period": {"all": ctx.dates, "train": ctx.train_dates, "oos": ctx.oos_dates, "warmup": ctx.warmup},
        "trades_A": [vars(t) for t in trades_a],
        "decisions": decisions,
    }, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    # 진입 동결 검증: 동결 재생이 A를 그대로 복원하는가
    trades_a2, _ = run_policy(ctx, chop_cfg=None, chop_tp_pct=None, frozen=decisions)
    same = ([(t.entry_time, t.entry_symbol, t.exit_time, round(t.net_pct, 6)) for t in trades_a]
            == [(t.entry_time, t.entry_symbol, t.exit_time, round(t.net_pct, 6)) for t in trades_a2])
    print(f"진입동결 재생이 A를 완전 복원: {same}")
    assert same, "frozen-decision replay does not reproduce policy A exactly"
    print(f"saved {OUTPUT_DIR / 'baseline_A.json'}")
    return 0


GRID = [ChopConfig(c, f, k) for c in (1, 2, 3) for f in (1, 2, 3) for k in (1, 2, 3)]


if __name__ == "__main__":
    stage = sys.argv[1] if len(sys.argv) > 1 else "prep"
    if stage == "prep":
        raise SystemExit(stage_prep())
    raise SystemExit(f"unknown stage: {stage}")
