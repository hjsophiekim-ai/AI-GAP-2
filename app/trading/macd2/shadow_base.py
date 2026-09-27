"""SHADOW-BASE — 주문 없는 가상 BASE 추적 (2026-09-27).

답해야 하는 질문: **"P3/B3/Y3 가 전혀 없었다면 지금 BASE(N1 + C1 + SMART +
AR1)는 무슨 거래를 하고 있을까?"** 그 가상 거래열이 ``chop_regime`` detector 의
유일한 입력이다.

왜 실거래가 아니라 가상거래인가
--------------------------------
B3 는 CHOP 포지션을 +1%/-1%/20분에 끊는다. 그 결과(= TP1 미도달, H50 개입
없음)를 다시 detector 에 먹이면 detector 가 스스로 CHOP 을 지운다. 2026-09-25
연구(P1)에서 이 피드백으로 9월 탐지율이 98.3% -> 3.6% 로 붕괴했다. SHADOW 는
실제 주문과 완전히 분리되어 있으므로 그 루프가 **구조적으로** 불가능하다.

실거래 스트림을 왜 그대로 못 쓰는가 / 왜 후보를 놓치지 않는가
-------------------------------------------------------------
B3 가 일찍 끊으면 슬롯이 일찍 비어 실거래는 BASE 가 하지 않았을 진입을 한다
(knock-on). 그래서 섀도우는 **자기 슬롯 장부**로 진입 여부를 따로 판정한다.

반대로 "BASE 는 진입했는데 섀도우가 후보 자체를 못 본" 경우는 생기지 않는다.
B3 의 청산은 전부 기존 청산보다 **이르거나 같다**(TP 1.0% < TP1 3.0%,
SL 1.0% < 손절 1.7%, max-hold 20분; 승격되면 기존 래더로 복귀). 즉

    BASE 가 flat 인 시점 ⊆ 실거래가 flat 인 시점

이고, 실거래는 flat 일 때 확정 플래그를 항상 평가하므로 BASE 가 잡을 수 있는
후보는 전부 이 모듈에 전달된다.

무엇을 재구현하지 않는가
------------------------
판정식을 새로 쓰지 않는다. 진입은 ``time_window_3slot.resolve_slot``, 청산은
``time_window_position_manager`` / ``small_whipsaw_hold`` / ``early_take_profit``
/ ``peak_protection`` / ``n1_adaptive`` 를 **production 과 같은 인자로** 부른다.
이 모듈이 가진 것은 가상 포지션의 장부와 호출 **순서**뿐이고, 그 순서는 worker
의 청산 체인과 같다: 틱 익절 -> 완성봉 래더 -> 조기익절 -> C1.

수량은 모델링하지 않는다. detector 가 보는 것은 ``h50_intervened`` 와
``tp1_hit`` 두 불리언뿐이고 둘 다 가격경로와 래더만으로 결정되므로, 섀도우는
단위 수량(1.0)으로 계산한다 -- SMART sizing 은 섀도우에 영향이 없다.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Optional

from app.trading.macd2 import (
    chop_regime,
    config,
    early_take_profit,
    n1_adaptive,
    order_executor,
    peak_protection,
    small_whipsaw_hold,
    time_window_3slot,
    time_window_position_manager,
)
from app.trading.macd2.models import Direction

KST = config.KST

EXIT_OPPOSITE_SIGNAL = "OPPOSITE_SIGNAL"
EXIT_FORCED_LIQUIDATION = config.EXIT_FORCED_LIQUIDATION
EXIT_H50_RELEASE = small_whipsaw_hold.EXIT_SMALL_WHIPSAW_HOLD


# ── 가상 포지션 ───────────────────────────────────────────────────────────
@dataclass
class ShadowPosition:
    """주문 없는 가상 포지션. 실제 브로커/원장과 아무 접점이 없다."""

    symbol: str
    direction: str
    entry_at: str
    entry_price: float
    session: str
    slot: Optional[int] = None
    trading_date: str = ""
    entry_reason: str = ""
    #: 남은 비중(1.0 -> TP1 부분익절 후 감소). 수량이 아니라 **비중**이다.
    qty_frac: float = 1.0
    #: 이미 실현된 손익(가중 %). 부분익절 누적.
    realized_pct: float = 0.0
    tp1_done: bool = False
    peak_net_pct: float = 0.0
    #: detector 가 보는 두 불리언.
    h50_intervened: bool = False
    tp1_hit: bool = False
    #: H50 HOLD 진행상태(실거래 state 와 분리된 섀도우 전용 사본).
    h50_hold_active: bool = False
    h50_hold_started_at: Optional[str] = None
    h50_trend_break_count: int = 0
    h50_last_checked_bar_ts: Optional[str] = None
    #: n1_adaptive 캐시(완성봉마다 갱신).
    n1_tp1: Optional[float] = None
    n1_ratio: Optional[float] = None
    n1_tp2: Optional[float] = None
    n1_last_eval_bar_ts: Optional[str] = None
    #: 마지막으로 평가한 완성봉(중복 평가 방지).
    last_bar_ts: Optional[str] = None
    entry_chop: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @staticmethod
    def from_dict(raw: Any) -> Optional["ShadowPosition"]:
        if not isinstance(raw, dict) or not raw.get("symbol"):
            return None
        fields = {f.name for f in dataclasses.fields(ShadowPosition)}
        try:
            return ShadowPosition(**{k: v for k, v in raw.items() if k in fields})
        except (TypeError, ValueError):
            return None


@dataclass
class ShadowBook:
    """섀도우의 하루 장부 — 실거래 슬롯 카운터와 **분리된** 사본."""

    trading_date: str = ""
    slots_used_today: int = 0
    morning_count: int = 0
    afternoon_count: int = 0
    last_afternoon_direction: Optional[str] = None
    position: Optional[ShadowPosition] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trading_date": self.trading_date,
            "slots_used_today": int(self.slots_used_today),
            "morning_count": int(self.morning_count),
            "afternoon_count": int(self.afternoon_count),
            "last_afternoon_direction": self.last_afternoon_direction,
            "position": self.position.to_dict() if self.position else None,
        }

    @staticmethod
    def from_dict(raw: Any) -> "ShadowBook":
        if not isinstance(raw, dict):
            return ShadowBook()
        return ShadowBook(
            trading_date=str(raw.get("trading_date") or ""),
            slots_used_today=int(raw.get("slots_used_today") or 0),
            morning_count=int(raw.get("morning_count") or 0),
            afternoon_count=int(raw.get("afternoon_count") or 0),
            last_afternoon_direction=raw.get("last_afternoon_direction") or None,
            position=ShadowPosition.from_dict(raw.get("position")),
        )


# ── state 부착 ────────────────────────────────────────────────────────────
def load_book(state) -> ShadowBook:
    return ShadowBook.from_dict(getattr(state, "p3_shadow", None))


def store_book(state, book: ShadowBook) -> None:
    state.p3_shadow = book.to_dict()


def is_active(state) -> bool:
    """섀도우를 돌려야 하는가.

    P3 토글이 켜져 있고 N1 계열일 때만 돈다. P3 가 꺼져 있으면 이 모듈은 단 한
    줄도 실행되지 않는다 -- OFF parity 의 근거다.
    """
    from app.trading.macd2 import p3_stack

    return p3_stack.is_active(state)


# ── 보조 ──────────────────────────────────────────────────────────────────
def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat() if dt is not None else None


def _parse(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    try:
        return datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return None


def _direction_of(pos: ShadowPosition) -> Optional[Direction]:
    try:
        return Direction(pos.direction)
    except (TypeError, ValueError):
        return None


def _net_pct(symbol: str, entry_price: float, price: float) -> float:
    """순수익률(%) — worker._net_return_pct 와 같은 계산을 쓴다.

    지연 import 로 순환참조를 피한다(worker 가 이 모듈을 import 한다).
    """
    from app.trading.macd2.worker import _net_return_pct

    return float(_net_return_pct(symbol, float(entry_price), float(price), 1))


def _session_of(now: datetime) -> str:
    from app.trading.macd2 import time_window_filter

    return time_window_filter.session_for_window(
        time_window_filter.classify_window(now.astimezone(KST).time()))


def _bar_ts_iso(bars_3m, idx: int) -> Optional[str]:
    try:
        ts = bars_3m["datetime"].iloc[idx]
    except Exception:
        return None
    return ts.isoformat() if hasattr(ts, "isoformat") else str(ts)


def _ladder_kwargs(state, pos: ShadowPosition) -> tuple[dict, Optional[float], dict]:
    """worker 의 ``_tw_exit_overrides`` / ``_n1_over`` 와 같은 값을 만든다."""
    mode = getattr(state, "time_window_active_mode", None) or \
        time_window_3slot.active_3slot_mode(state)
    overrides = time_window_3slot.exit_overrides(mode)
    tp2 = time_window_3slot.morning_tp2_pct_override(mode)
    n1_over: dict = {}
    if n1_adaptive.is_active(state):
        if pos.n1_tp1 is None:
            tp1_d, ratio_d, tp2_d = n1_adaptive.off_trend_ladder()
        else:
            tp1_d, ratio_d, tp2_d = pos.n1_tp1, pos.n1_ratio, pos.n1_tp2
        n1_over = {
            "tp2_pct_override": float(tp2_d),
            "tp1_sell_ratio_override": float(ratio_d),
            "tp1_pct_override": float(tp1_d),
        }
    return overrides, tp2, n1_over


# ── 완료 기록 ─────────────────────────────────────────────────────────────
def _close(book: ShadowBook, pos: ShadowPosition, *, exit_at: datetime,
           exit_price: float, exit_reason: str, log=None) -> chop_regime.ShadowTrade:
    """가상 포지션을 닫고 완료거래를 ledger 에 기록한다.

    ``net_pct`` 는 부분익절 누적(realized_pct) + 잔량 비중 × 최종 순수익이다 --
    실거래 원장의 가중 손익과 같은 방식이다.
    """
    final_net = _net_pct(pos.symbol, pos.entry_price, exit_price)
    net = float(pos.realized_pct) + float(pos.qty_frac) * final_net
    trade = chop_regime.ShadowTrade(
        shadow_trade_id=chop_regime.make_shadow_trade_id(
            pos.entry_at, pos.direction, pos.slot),
        trading_date=pos.trading_date,
        entry_time=pos.entry_at,
        exit_time=exit_at.isoformat(),
        direction=pos.direction,
        slot=pos.slot,
        entry_price=float(pos.entry_price),
        exit_price=float(exit_price),
        net_pct=float(net),
        h50_intervened=bool(pos.h50_intervened),
        tp1_hit=bool(pos.tp1_hit),
        entry_reason=pos.entry_reason,
        exit_reason=str(exit_reason),
        completed=True,
        shadow_only=True,
    )
    book.position = None
    try:
        chop_regime.append_trade(trade)
    except Exception as exc:  # noqa: BLE001 -- 섀도우 기록 실패가 실거래를 막지 않는다
        if log is not None:
            log("SHADOW_ERROR_FALLBACK", reason=f"append_failed:{type(exc).__name__}")
        return trade
    if log is not None:
        log("SHADOW_EXIT", trade_id=trade.shadow_trade_id, direction=trade.direction,
            entry_time=trade.entry_time, net=round(trade.net_pct, 4),
            reason=exit_reason, mode="SHADOW")
    return trade


# ── 일자 롤오버 ───────────────────────────────────────────────────────────
def on_day_rollover(state, now: datetime, *, log=None) -> None:
    """날짜가 바뀌면 섀도우 하루 장부를 초기화한다.

    열려 있던 가상 포지션은 당일 강제청산으로 닫는다 -- 실거래의
    FORCE_LIQUIDATE_AT 와 같은 취급이다(1박 보유는 BASE 에도 없다).
    """
    book = load_book(state)
    day = now.astimezone(KST).strftime("%Y%m%d")
    if book.trading_date == day:
        return
    if book.position is not None:
        _close(book, book.position, exit_at=now,
               exit_price=float(book.position.entry_price),
               exit_reason=EXIT_FORCED_LIQUIDATION, log=log)
    store_book(state, ShadowBook(trading_date=day))


# ── 완성봉 갱신(n1_adaptive 캐시) ─────────────────────────────────────────
def _advance_n1_cache(pos: ShadowPosition, state, bars_3m, bar_ts_iso: Optional[str]) -> None:
    if not n1_adaptive.is_active(state) or bar_ts_iso is None:
        return
    if pos.n1_last_eval_bar_ts and bar_ts_iso <= pos.n1_last_eval_bar_ts:
        return
    held = _direction_of(pos)
    if held is None:
        return
    try:
        decision = n1_adaptive.resolve_ladder(bars_3m, held)
    except Exception:
        return
    pos.n1_last_eval_bar_ts = bar_ts_iso
    pos.n1_tp1 = float(decision.tp1_pct)
    pos.n1_ratio = float(decision.tp1_sell_ratio)
    pos.n1_tp2 = float(decision.tp2_pct)


# ── 청산 진행 ─────────────────────────────────────────────────────────────
def advance_exits(state, *, now: datetime, quotes: dict, bars_3m=None,
                  completed_bar_close: Optional[float] = None,
                  completed_bar_idx: Optional[int] = None, log=None) -> None:
    """열린 가상 포지션에 BASE 청산 래더를 적용한다.

    호출 순서는 worker 의 청산 체인과 같다:
    강제청산 -> 틱 익절 -> 완성봉 래더 -> 조기익절 -> C1.
    (OPPOSITE_SIGNAL / H50 / whipsaw 는 확정 플래그가 있을 때
    :func:`on_confirmed_flag` 에서 처리한다 -- worker 와 같은 분업이다.)
    """
    book = load_book(state)
    pos = book.position
    if pos is None:
        return
    price = quotes.get(pos.symbol)
    if price is None or float(price) <= 0:
        store_book(state, book)
        return

    # ① 강제청산 (15:00)
    if now.astimezone(KST).time() >= config.FORCE_LIQUIDATE_AT:
        _close(book, pos, exit_at=now, exit_price=float(price),
               exit_reason=EXIT_FORCED_LIQUIDATION, log=log)
        store_book(state, book)
        return

    tick_net = _net_pct(pos.symbol, pos.entry_price, float(price))
    pos.peak_net_pct = max(float(pos.peak_net_pct), tick_net)

    bar_ts_iso = (_bar_ts_iso(bars_3m, completed_bar_idx)
                  if (bars_3m is not None and completed_bar_idx is not None) else None)
    _advance_n1_cache(pos, state, bars_3m, bar_ts_iso)
    overrides, tp2_mode, n1_over = _ladder_kwargs(state, pos)

    # ② 틱 익절 (TP1 / TP2 / 오후TP)
    tp = time_window_position_manager.evaluate_take_profit_immediate(
        session=pos.session or "MORNING",
        net_return_pct=tick_net,
        tp1_done=bool(pos.tp1_done),
        tp2_pct_override=n1_over.get("tp2_pct_override", tp2_mode),
        afternoon_tp_pct_override=overrides["afternoon_tp_pct_override"],
        tp1_sell_ratio_override=n1_over.get(
            "tp1_sell_ratio_override", overrides["tp1_sell_ratio_override"]),
        tp1_pct_override=n1_over.get("tp1_pct_override"),
    )
    if tp.exit_reason is not None:
        pos.peak_net_pct = max(float(pos.peak_net_pct), float(tp.peak_net_return))
        frac = max(0.0, min(1.0, float(tp.sell_fraction)))
        if frac >= 1.0:
            _close(book, pos, exit_at=now, exit_price=float(price),
                   exit_reason=tp.exit_reason, log=log)
            store_book(state, book)
            return
        # 부분익절 = TP1 도달. detector 가 보는 tp1_hit 이 여기서 확정된다.
        pos.realized_pct += float(pos.qty_frac) * frac * tick_net
        pos.qty_frac = float(pos.qty_frac) * (1.0 - frac)
        pos.tp1_done = bool(tp.tp1_done)
        pos.tp1_hit = True
        store_book(state, book)
        return

    # ③ 완성봉 래더 (손절 / after-TP1 스탑 / trailing / breakeven)
    if completed_bar_close is None or (bar_ts_iso and pos.last_bar_ts == bar_ts_iso):
        store_book(state, book)
        return
    pos.last_bar_ts = bar_ts_iso
    bar_net = _net_pct(pos.symbol, pos.entry_price, float(completed_bar_close))
    pm_kw = dict(overrides)
    pm_tp2 = tp2_mode
    if n1_over:
        pm_tp2 = n1_over["tp2_pct_override"]
        pm_kw["tp1_sell_ratio_override"] = n1_over["tp1_sell_ratio_override"]
    pm = time_window_position_manager.evaluate_position(
        session=pos.session or "MORNING",
        net_return_pct=bar_net,
        tp1_done=bool(pos.tp1_done),
        peak_net_return=float(pos.peak_net_pct),
        tp2_pct_override=pm_tp2,
        tp1_pct_override=n1_over.get("tp1_pct_override"),
        **pm_kw,
    )
    pos.peak_net_pct = max(float(pos.peak_net_pct), float(pm.peak_net_return), bar_net)
    if pm.exit_reason is not None:
        frac = max(0.0, min(1.0, float(pm.sell_fraction)))
        if frac >= 1.0:
            _close(book, pos, exit_at=now, exit_price=float(completed_bar_close),
                   exit_reason=pm.exit_reason, log=log)
            store_book(state, book)
            return
        pos.realized_pct += float(pos.qty_frac) * frac * bar_net
        pos.qty_frac = float(pos.qty_frac) * (1.0 - frac)
        pos.tp1_done = bool(pm.tp1_done)
        pos.tp1_hit = True
        store_book(state, book)
        return

    # ④ 조기익절 (ETP)
    if early_take_profit.is_active(state):
        trig, floor = early_take_profit.thresholds(state)
        etp = early_take_profit.evaluate(
            entry_chop=bool(pos.entry_chop),
            peak_net_return_pct=float(pos.peak_net_pct),
            net_return_pct=bar_net,
            trigger_pct=trig, floor_pct=floor,
        )
        if etp.exit_reason is not None:
            _close(book, pos, exit_at=now, exit_price=float(completed_bar_close),
                   exit_reason=etp.exit_reason, log=log)
            store_book(state, book)
            return

    # ⑤ C1 Peak Protection
    if peak_protection.is_active(state):
        hist = _macd_hist_at(bars_3m, completed_bar_idx)
        c1 = peak_protection.evaluate(
            held_direction=_direction_of(pos),
            peak_net_return_pct=float(pos.peak_net_pct),
            net_return_pct=bar_net,
            macd_hist=hist,
        )
        if c1.exit_reason is not None:
            _close(book, pos, exit_at=now, exit_price=float(completed_bar_close),
                   exit_reason=c1.exit_reason, log=log)
            store_book(state, book)
            return

    store_book(state, book)


def _macd_hist_at(bars_3m, idx: Optional[int]) -> Optional[float]:
    """완성봉 ``idx`` 의 MACD 히스토그램. C1 의 gap 반전 판정용."""
    if bars_3m is None or idx is None:
        return None
    from app.trading.macd2 import p3_stack

    hist = p3_stack.macd_hist_series(bars_3m)
    if hist is None or idx < 0 or idx >= len(hist):
        return None
    return float(hist[idx])


# ── 확정 플래그 처리 (H50 / OPPOSITE / 진입) ──────────────────────────────
def on_confirmed_flag(state, *, direction: Direction, now: datetime,
                      quotes: dict, bars_3m=None, entry_price: Optional[float] = None,
                      slot_approved_hint: Optional[bool] = None,
                      entry_chop: bool = False, log=None) -> None:
    """확정 플래그 한 건에 대해 섀도우가 할 일.

    1. 가상 포지션이 **같은 방향**이면 아무 것도 하지 않는다.
    2. 반대방향이면 H50 을 먼저 묻는다 -- HOLD 면 ``h50_intervened`` 를 켜고
       포지션을 유지한다(이것이 detector 가 세는 "개입"이다).
       HOLD 가 아니면 OPPOSITE_SIGNAL 로 닫는다.
    3. flat 이면 **섀도우 자신의 슬롯 장부**로 진입 여부를 판정한다.
    """
    book = load_book(state)
    target = order_executor.target_symbol_for_direction(direction)
    pos = book.position

    if pos is not None:
        if pos.symbol == target:
            store_book(state, book)
            return
        held = _direction_of(pos)
        if small_whipsaw_hold.is_active(state):
            try:
                h50 = small_whipsaw_hold.evaluate_hold(bars_3m, held, now)
            except Exception:
                h50 = None
            if h50 is not None and h50.should_hold:
                # detector 가 세는 개입. 같은 포지션에서 여러 번 일어나도
                # 불리언 하나로 남는다(연구 필드 h50_held 와 동일).
                pos.h50_intervened = True
                if not pos.h50_hold_active:
                    pos.h50_hold_active = True
                    pos.h50_hold_started_at = _iso(now)
                    pos.h50_trend_break_count = 0
                store_book(state, book)
                return
        price = quotes.get(pos.symbol)
        if price is None or float(price) <= 0:
            store_book(state, book)
            return
        _close(book, pos, exit_at=now, exit_price=float(price),
               exit_reason=EXIT_OPPOSITE_SIGNAL, log=log)
        store_book(state, book)
        # 반대신호로 닫힌 직후의 진입(switch)은 아래 flat 경로에서 이어 판정한다.
        pos = None

    # flat -- 섀도우 슬롯 장부로 진입 판정
    day = now.astimezone(KST).strftime("%Y%m%d")
    if book.trading_date != day:
        book = ShadowBook(trading_date=day)
    slot = time_window_3slot.resolve_slot(
        now=now,
        slots_used_today=int(book.slots_used_today),
        morning_count=int(book.morning_count),
        afternoon_count=int(book.afternoon_count),
        direction=direction,
        is_flat=True,
        last_afternoon_direction=book.last_afternoon_direction,
    )
    allowed = bool(slot.slot_allowed)
    if (not allowed
            and slot.reject_reason == time_window_3slot.REJECT_SAME_DIRECTION_AFTERNOON
            and time_window_3slot.afternoon_reentry_exception_enabled(state)
            and bool(slot_approved_hint)):
        # AR1 -- 실거래 경로가 이미 같은 후보에 대해 AR1 을 통과시켰다면
        # 섀도우도 같은 예외를 적용한다. AR1 의 게이트/조건은 손대지 않는다
        # (worker 가 계산한 결과를 그대로 받는다).
        allowed = True
    if not allowed:
        store_book(state, book)
        return

    px = entry_price if entry_price is not None else quotes.get(target)
    if px is None or float(px) <= 0:
        store_book(state, book)
        return

    session = slot.session or _session_of(now)
    book.position = ShadowPosition(
        symbol=target,
        direction=direction.value,
        entry_at=now.isoformat(),
        entry_price=float(px),
        session=session,
        slot=slot.slot_number,
        trading_date=day,
        entry_reason="SHADOW_CONFIRM",
        entry_chop=bool(entry_chop),
    )
    book.slots_used_today = int(book.slots_used_today) + 1
    if session == time_window_3slot.SESSION_AFTERNOON:
        book.afternoon_count = int(book.afternoon_count) + 1
        book.last_afternoon_direction = direction.value
    else:
        book.morning_count = int(book.morning_count) + 1
    store_book(state, book)
    if log is not None:
        log("SHADOW_ENTRY",
            trade_id=chop_regime.make_shadow_trade_id(
                book.position.entry_at, book.position.direction, book.position.slot),
            direction=direction.value, entry_time=book.position.entry_at,
            mode="SHADOW", reason=f"slot{slot.slot_number}/{session}")


def advance_h50_release(state, *, now: datetime, quotes: dict, bars_3m=None,
                        bar_ts_iso: Optional[str] = None, log=None) -> None:
    """섀도우 H50 HOLD 의 해제 판정 -- 완성봉마다.

    해제되면 BASE 와 마찬가지로 ``SMALL_WHIPSAW_HOLD_EXIT`` 로 닫는다.
    """
    book = load_book(state)
    pos = book.position
    if pos is None or not pos.h50_hold_active:
        return
    if bar_ts_iso and pos.h50_last_checked_bar_ts == bar_ts_iso:
        return
    pos.h50_last_checked_bar_ts = bar_ts_iso
    held = _direction_of(pos)
    try:
        rel = small_whipsaw_hold.evaluate_release(
            bars_3m, held, _parse(pos.h50_hold_started_at), now,
            int(pos.h50_trend_break_count))
    except Exception:
        store_book(state, book)
        return
    pos.h50_trend_break_count = int(rel.trend_break_count)
    if not rel.should_release:
        store_book(state, book)
        return
    price = quotes.get(pos.symbol)
    if price is None or float(price) <= 0:
        store_book(state, book)
        return
    _close(book, pos, exit_at=now, exit_price=float(price),
           exit_reason=EXIT_H50_RELEASE, log=log)
    store_book(state, book)
