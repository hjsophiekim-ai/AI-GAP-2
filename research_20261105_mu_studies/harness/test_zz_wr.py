"""[2026-10-30 ARM3(+3% 틱 보호선) · MU Assist/XS · 실틱 재생 연구 사본]\n원본: research_20261029_n1_morning_stop_p70r/harness/test_zz_par.py\nproduction 무수정. 덧붙인 것만 있다 — 자세한 내용은 scripts/h0_make.py 참조.\n\n── 이하 원본 docstring ──\n[2026-10-29 live↔replay PARITY + P70R 연구 사본]\n원본: research_20261020_h50_hold/harness/test_zz_h50.py — 변형 PAR / PAR70 / PAR50 만 추가.\nproduction 무수정. 자세한 내용은 scripts/make_parity_harness.py 참조.\n\n── 이하 원본 docstring ──\n[2026-10-20 H50/SMALL_WHIPSAW 연구 사본] 원본: research_20261013_opp_quality_hold/harness/test_zz_v6.py
VAR=ECHOP / ECHOPB / ECHOPC 세 변형만 추가했고 나머지는 원본 그대로다.

v4 (2026-10-12): E/CHOP production replay. 원본 = E production parity: wk17 에서 연구용 RSBRK 흉내를 끄고 production MODE_E 를 그대로 돌린다.
VAR=EPROD -> strategy_mode E (+ config.E_RS_TABLE_PATH = RS_TABLE, 기준가 = 연구와 같은 비반올림 보간)
VAR=A     -> strategy_mode P3 (OFF parity)
원본: — 9월 전략 비교용 production worker 재생 하네스 v2. 모든 쓰기 tmp, production 코드 무수정.

env:
  WK_D / WK_P / WK_P2 : 재생일 / 전일(선재생) / 전전일(지표 워밍업 데이터)
  WK_FILL  : OLD  = 기존 하네스와 동일 (시세 = 직전 완성 1분봉 종가, FakeBroker: IOC 는 지정가 체결, 시장가 매도 = 시세)
             REAL = 시세 = 그 순간 가격(1분봉 시가 -> 종가 선형, 20초 tick), 호가 1틱(5원) 스프레드
                    ask1 = 시세+5, bid1 = 시세. production 주문(IOC 지정가 = ask1+1틱)은 ask1 에 체결,
                    시장가 매도는 bid1 에 체결. 지정가 < ask1 이면 미체결.
  WK_VAR   : A = P3 그대로 / B = N1 그대로
             C = N1 + 청산단순화(틱 TP/TP1/TP2/오후TP/trailing/after-TP1/ETP/C1 제거, N1 손절 유지,
                 보유 중 반대 확인플래그는 진입게이트 '거절' 처리 -> production 의 청산전용 경로, 반대 ETF 즉시매수 없음)
             D = C + 손절 제거
             C0/D0 = C/D 에서 H50 OFF (MACD2_H50_ENABLED 는 import 시점 상수라 config 를 직접 False)
  WK_OUT   : 출력 json 경로
"""
from __future__ import annotations

import dataclasses
import json
import os
import pickle
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from app.trading.macd2 import (chop_regime, config, early_take_profit, ledger, p3_stack, premarket_shadow,
                               state_store, strategy_mode, time_window_filter, time_window_position_manager as tpm,
                               worker)
from app.trading.macd2.market_data import MarketDataService
from app.trading.macd2.models import Direction, QuoteSnapshot
from app.trading.macd2.broker_adapter import BrokerOrderResult
from tests.macd2.fake_broker import FakeBroker

SP = Path(os.environ.get("REPLAY_SP", ""))
KST = config.KST
ETF_TICK = 5.0


# ── 2026-10-29 PARITY 파라미터 (env. 전부 0/False 면 기존 하네스와 동일) ──
_HIST_LAG = timedelta(seconds=int(os.environ.get("WK_HIST_LAG_SEC", "0") or 0))
_FILL_LAG = timedelta(seconds=int(os.environ.get("WK_FILL_LAG_SEC", "0") or 0))
_FINE_TICK = os.environ.get("WK_FINE_TICK", "0") == "1"
#: 루프 틱 간격. production WORKER_INTERVAL_SEC=5.0 이 실제값이고, 기존 하네스는 20초였다.
#: 20초 격자는 가격구동 결정(E 돌파 / 틱 익절)을 최대 20초 일찍 터뜨린다.
_TICK_SEC = int(os.environ.get("WK_TICK_SEC", "20") or 20)


# ── 2026-10-30 ARM3 연구 파라미터 (env. 전부 기본값이면 PAR 하네스와 동일) ──
#: ws_shadow_logger 가 모은 1초 스냅샷 루트 (snapshots/<YYYY-MM-DD>/<종목>.csv).
_RT = Path(os.environ.get("ARM3_RT", "") or "")
#: 1 이면 ETF/하이닉스 시세·호가를 1분봉 선형보간이 아니라 **실제 1초 스냅샷**으로 준다.
_TICKPX = os.environ.get("WK_TICKPX", "0") == "1"
#: NONE / SOFT(B) / MEDIUM(C)
_ARM = (os.environ.get("WK_ARM", "NONE") or "NONE").upper()
#: 전략모드 — 비어 있으면 기존 VAR 분기 그대로.
_MODE = (os.environ.get("WK_MODE", "") or "").upper()
_MUA = os.environ.get("WK_MUA", "0") == "1"
_XS = os.environ.get("WK_XS", "0") == "1"
_CUKILL = os.environ.get("WK_CUKILL", "0") == "1"
#: 보호선 사다리 — 사용자 사전등록값. 키 = 틱 최고 순수익(%), 값 = 보호선(%).
_ARM_LADDER = {
    "SOFT":   ((4.0, 2.5), (3.5, 2.0), (3.0, 1.5)),
    "MEDIUM": ((4.0, 3.0), (3.5, 2.5), (3.0, 2.0)),
    # 2026-10-31 후속: 추세약화 확인형. 사다리는 SOFT 와 같고 **활성화 조건만** 다르다.
    "W1":     ((4.0, 2.5), (3.5, 2.0), (3.0, 1.5)),
    "W2":     ((4.0, 2.5), (3.5, 2.0), (3.0, 1.5)),
}
#: 보호 활성화에 필요한 추세약화 신호 개수. 0 = +3% 도달 즉시 활성(기존 SOFT/MEDIUM).
_ARM_NEED = {"SOFT": 0, "MEDIUM": 0, "W1": 1, "W2": 2}

# ── 2026-11-01 ⑧ CHOP 내부 차등 사이징 (S축 slot / E축 10시경계) ─────────
#: CHOP regime 진입에만 적용한다. TREND/WARMUP 은 항상 _BUD_TREND(기본 10M).
#: S축 — slot1 / slot2 예산. 0 이면 OFF.
_BUD_SLOT1 = float(os.environ.get("WK_BUD_SLOT1", "0") or 0)
_BUD_SLOT2 = float(os.environ.get("WK_BUD_SLOT2", "0") or 0)
#: E축 — 10시 이전 / 이후 예산. 0 이면 OFF.
_BUD_EARLY = float(os.environ.get("WK_BUD_EARLY", "0") or 0)
_BUD_LATE = float(os.environ.get("WK_BUD_LATE", "0") or 0)
#: E축 경계(분). 기본 600 = 10:00. 탐색하지 않는다.
_EARLY_CUT_MIN = int(os.environ.get("WK_EARLY_CUT_MIN", "600") or 600)

# ── 2026-11-01 ⑦ REGIME 별 자본배분 (REGALLOC) ───────────────────────────
#: 0 이면 OFF. 값이 있으면 진입 사이징 직전에 그 tick 의 regime 으로 state.budget 을 고른다.
#: regime 판정·진입게이트·청산은 전혀 바꾸지 않는다 — 바뀌는 것은 budget 하나뿐이다.
_BUD_TREND = float(os.environ.get("WK_BUD_TREND", "0") or 0)
_BUD_CHOP = float(os.environ.get("WK_BUD_CHOP", "0") or 0)

# ── 2026-10-31 ⑥ 분산 — 슬롯 상한 × 건당 예산 (수익·안전성 동시) ────────
#: 0 = OFF(production CHOP_DAILY_CAP=2 그대로). 값이 있으면 하루 슬롯 상한을 그 값으로.
_SLOTCAP = int(os.environ.get("WK_SLOT_CAP", "0") or 0)
#: 0 = OFF(하네스 기본 10,000,000). 값이 있으면 state.budget 을 그 값으로.
#: 슬롯을 늘리면서 건당 예산을 줄이면 **총 노출은 유지하고 단일거래 집중도만 낮춘다**.
_BUDGET = float(os.environ.get("WK_BUDGET", "0") or 0)

# ── 2026-10-31 ⑤ CHOP 진입 + N1 runner 승격 ───────────────────────────────
#: 1 이면 CHOP regime 진입 중 production n1_adaptive.snapshot().ok 인 것만
#: TREND 로 각인해 B3 스택 대신 N1 래더(runner)로 관리한다. 진입게이트는 불변.
_TRBP = os.environ.get("WK_TREND_BYPASS", "0") == "1"

# ── 2026-10-31 ④ ARM 3% ONLY — 트레일링 arm 시작점만 3.5% -> 3.0% ─────────
#: 0 = OFF. 값이 있으면 트레일링 arm 임계값(%)을 그 값으로 바꾼다. 보호선 자체
#: (N1_MORNING_TRAILING_STOP 1.5%)와 나머지 청산은 **한 줄도 바꾸지 않는다**.
_TARM = float(os.environ.get("WK_TRAIL_ARM", "0") or 0)

# ── 2026-10-31 ③ 오후 XS VWAP 근접 override ───────────────────────────────
#: 0 = OFF. 값이 있으면 그 값(%)을 VWAP '불리폭' 상한으로 쓴다 (0.10 / 0.20).
_VWOV = float(os.environ.get("WK_VWAP_OVR", "0") or 0)

# ── 2026-10-31 ② 청산 유예 (ONE_BAR_HOLD / TWO_BAR_HOLD) ──────────────────
#: NONE / ONE(B, 1봉) / TWO(C, 최대 2봉)
_HOLD = (os.environ.get("WK_HOLD", "NONE") or "NONE").upper()
_HOLD_BARS = {"ONE": 1, "TWO": 2}.get(_HOLD, 0)
#: 유예 **대상** 청산사유 — "1~2봉짜리 일시 휩쏘일 수 있는" 것만. 부분 문자열로 본다.
_HOLD_TARGET = (
    "OPPOSITE_SIGNAL", "TW2_3SLOT_SELL_ONLY",          # 반대 플래그 (진입 미승인분)
    "H50_WHIPSAW_RELEASE", "H50_MAX_HOLD_EXIT", "MU_ASSIST_H50_GAP_EXIT",
    "C1_PEAK_PROTECTION_EXIT", "TIME_WINDOW_TRAILING_STOP",
    "TIME_WINDOW_PROFIT_LOCK_STOP", "TIME_WINDOW_AFTER_TP1_STOP",
    "TIME_WINDOW_BREAKEVEN_STOP", "B3_MAXHOLD",
)
#: **절대 유예하지 않는다** — hard stop / 구조 확정이탈 / 익절 / 장마감.
_HOLD_NEVER = (
    "STOP_LOSS", "B3_SL", "FORCED_LIQUIDATION", "TP1", "TP2", "B3_TP",
    "AFTERNOON_TP", "P3_PARTIAL", "TICK_ARM3_STOP", "RUNNER",
)


def _arm_line(peak):
    """틱 최고 순수익 peak(%) 에 대응하는 보호선(%). 사다리 밖이면 None.

    +5% 이상에서 보호선을 더 올리지 않는다 — 최고 단은 peak>=4.0 하나뿐이다.
    그 위 구간은 기존 TP/C1 러너 규칙이 그대로 지배한다(사용자 사전등록)."""
    for lv, line in _ARM_LADDER.get(_ARM, ()):
        if peak >= lv:
            return line
    return None


_TICK_CACHE = {}


def _tick_df(sym, day):
    """그 날 1초 스냅샷. ts(naive KST) index, price/best_bid/best_ask."""
    key = (sym, day)
    if key not in _TICK_CACHE:
        p = _RT / "snapshots" / day / f"{sym}.csv"
        if not _RT or not p.exists():
            _TICK_CACHE[key] = None
        else:
            df = pd.read_csv(p, usecols=["ts", "price", "best_bid", "best_ask"])
            df["ts"] = pd.to_datetime(df["ts"])
            df = df.dropna(subset=["price"]).drop_duplicates("ts", keep="last")
            _TICK_CACHE[key] = df.set_index("ts").sort_index()
    return _TICK_CACHE[key]


def _tick_row(sym, now, max_age_sec=90):
    """now 시점에 **이미 수신된** 마지막 1초 스냅샷. 미래 데이터는 쓰지 않는다."""
    ts = pd.Timestamp(now)
    nv = ts.tz_localize(None) if ts.tzinfo is not None else ts
    df = _tick_df(sym, nv.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return None
    i = df.index.searchsorted(nv, side="right") - 1
    if i < 0 or (nv - df.index[i]).total_seconds() > max_age_sec:
        return None
    return df.iloc[i]


def _tick_window(sym, t0, t1):
    """(t0, t1] 구간의 1초 스냅샷 전부 — 틱 최고가/보호선 이탈 판정용."""
    a, b = pd.Timestamp(t0), pd.Timestamp(t1)
    a = a.tz_localize(None) if a.tzinfo is not None else a
    b = b.tz_localize(None) if b.tzinfo is not None else b
    df = _tick_df(sym, b.strftime("%Y-%m-%d"))
    if df is None or df.empty:
        return None
    w = df[(df.index > a) & (df.index <= b)]
    return w if len(w) else None


def _d(s):
    return datetime.strptime(s, "%Y%m%d").replace(tzinfo=KST)


class RealFillBroker(FakeBroker):
    """호가 1틱 스프레드 모형: ask1 = 시세 + 5원, bid1 = 시세."""

    #: 실제 1초 스냅샷 최우선호가가 있으면 그것을 쓴다 (ARM3 하네스가 채운다).
    book = None

    def _ask(self, symbol):
        bk = (self.book or {}).get(symbol) if self.book is not None else None
        if bk and bk[1]:
            return float(bk[1])
        q = self._quotes.get(symbol)
        return None if q is None else float(q) + ETF_TICK

    def get_fresh_ask1(self, symbol):
        a = self._ask(symbol)
        return {"ok": bool(a), "symbol": symbol, "ask1": float(a or 0.0), "rt_cd": "0" if a else "1",
                "msg_cd": "RF_ASK_OK" if a else "RF_ASK_MISSING", "msg1": "realfill ask"}

    def get_buy_sizing_quote(self, symbol, *, price, order_type="market"):
        q = super().get_buy_sizing_quote(symbol, price=price, order_type=order_type)
        return dataclasses.replace(q, ask1=float(self._ask(symbol) or 0.0))

    def _fill_buy(self, symbol, qty, px, dvsn):
        fill_qty = qty
        self._cash -= px * fill_qty
        ex = self._positions.get(symbol)
        from app.models import Position
        if ex:
            tq = ex.quantity + fill_qty
            self._positions[symbol] = Position(symbol=symbol, name=symbol, quantity=tq,
                                               avg_price=(ex.avg_price * ex.quantity + px * fill_qty) / tq, current_price=px)
        else:
            self._positions[symbol] = Position(symbol=symbol, name=symbol, quantity=fill_qty, avg_price=px, current_price=px)
        r = BrokerOrderResult(True, self._next_order_id(), symbol, "BUY", qty, fill_qty, px, "OK", raw={"ORD_DVSN": dvsn})
        self.orders.append(r)
        return r

    def buy_market(self, symbol, qty, client_order_id):
        a = self._ask(symbol)
        if a is None or qty < 1:
            return super().buy_market(symbol, qty, client_order_id)
        return self._fill_buy(symbol, qty, a, "01")

    def buy_ioc_limit(self, symbol, qty, price, client_order_id):
        a = self._ask(symbol)
        if a is None or qty < 1 or float(price) < a:
            r = BrokerOrderResult(False, self._next_order_id(), symbol, "BUY", qty, 0, 0.0, "RF_IOC_NO_FILL",
                                  raw={"rt_cd": "1", "msg1": "ioc limit below ask", "ORD_DVSN": "11"})
            self.orders.append(r)
            return r
        return self._fill_buy(symbol, qty, a, "11")

    def buy_limit(self, symbol, qty, price, client_order_id):
        return self.buy_ioc_limit(symbol, qty, price, client_order_id)
    # sell_market: FakeBroker 그대로 = 시세(bid1) 체결


def test_wk2(monkeypatch, tmp_path):
    D, P, P2 = os.environ["WK_D"], os.environ["WK_P"], os.environ["WK_P2"]
    FILL, VAR = os.environ.get("WK_FILL", "OLD"), os.environ.get("WK_VAR", "A")
    monkeypatch.setattr(chop_regime, "LEDGER_DIR_PATH", tmp_path)
    monkeypatch.setattr(chop_regime, "LEDGER_PATH", tmp_path / "shadow.json")
    monkeypatch.setattr(premarket_shadow, "PREMARKET_CARRY_DIR", tmp_path / "pm")
    monkeypatch.setattr(worker, "_git_sha", lambda: "replay")

    # ── MUCONF (2026-11-03 연구) — MU ENTRY_CONFIRM 일 때만 진입 ─────────────────
    # WK_MUCONF=1 : T+2 기록 상태가 ENTRY_CONFIRM 이 아니면(WAIT/DIVERGE/BAD/판정불가/기록없음)
    # 진입을 막는다. 사유를 BAD 와 다르게 둬서 CATCH-UP 예외로 풀리지 않게 한다.
    MUCLOG = []
    if os.environ.get("WK_MUCONF", "") == "1":
        from app.trading.macd2 import mu_assist as _mua_c

        def _confirm_only(signal_id):
            st = _mua_c.entry_status_for(signal_id)
            MUCLOG.append(dict(sid=str(signal_id), status=st))
            return None if st == _mua_c.ENTRY_CONFIRM else "MU_CONFIRM_ONLY"
        monkeypatch.setattr(_mua_c, "entry_veto_reason", _confirm_only)

        def _muc_dump():
            Path(os.environ["WK_OUT"] + ".muc.json").write_text(
                json.dumps(MUCLOG, ensure_ascii=False, default=str), encoding="utf-8")
        import atexit as _atexit_mc
        _atexit_mc.register(_muc_dump)

    # ── MUHOLD (2026-11-03 연구) — MU 가 보유방향을 지지하면 N1/X2-lite 손절 유예 ──────
    # WK_MUHOLD=<하드스톱%> (예 -2.5). worker 실포지션 호출 + N1/X2-lite override 일 때만.
    # 손절 판정이 나온 봉에서 그 시각까지 완성된 MU 1분봉 MACD hist(보유방향 부호) > 0 이면 HOLD.
    # MU 판정 불가(결측/stale) 또는 hist<=0 또는 net<=하드스톱이면 원래대로 손절한다.
    MUHLOG = []
    _MUHOLD = os.environ.get("WK_MUHOLD", "")
    if _MUHOLD:
        import sys as _sys_h
        from app.trading.macd2 import time_window_position_manager as _twpm_h, mu_assist as _mua_h, order_executor as _oe_h
        _o_ep_h = _twpm_h.evaluate_position

        def _muhold_ep(**kw):
            fr = _sys_h._getframe(1)
            dec = _o_ep_h(**kw)
            caller = fr.f_code.co_filename.replace("\\", "/")
            n1x = kw.get("stop_loss_pct_override")
            if not (caller.endswith("app/trading/macd2/worker.py") and n1x is not None
                    and abs(float(n1x) - float(config.X2LITE_MORNING_STOP_LOSS) * 100.0) < 1e-9
                    and dec.exit_reason == config.EXIT_TW_STOP_LOSS):
                return dec
            net = float(kw.get("net_return_pct"))
            st, now = fr.f_locals.get("state"), fr.f_locals.get("now")
            rec = dict(t=str(now), net=round(net, 4), session=kw.get("session"))
            try:
                pos = st.position
                d = Direction.UP_RED if pos.symbol == _oe_h.target_symbol_for_direction(Direction.UP_RED) else Direction.DOWN_BLUE
                ser = _mua_h.load_mu_1m(now)
                stt = _mua_h._status(ser, _mua_h._naive(now).replace(second=0, microsecond=0) - timedelta(minutes=1))
                hist = None
                if stt == _mua_h.STATUS_OK:
                    hist = float(_mua_h._sign(d) * _mua_h._macd_hist(_mua_h._upto(ser, _mua_h._naive(now))).iloc[-1])
                rec.update(sym=pos.symbol, mu_status=stt, mu_hist=hist)
            except Exception as exc:          # 판정 불가 = 원래대로
                rec.update(err=repr(exc)); hist = None
            hold = hist is not None and hist > 0 and net > float(_MUHOLD)
            rec["hold"] = bool(hold)
            MUHLOG.append(rec)
            if hold:
                return _twpm_h.PositionManagementDecision(None, 0.0, bool(kw.get("tp1_done")),
                                                          dec.peak_net_return, "MU_HOLD")
            return dec
        monkeypatch.setattr(_twpm_h, "evaluate_position", _muhold_ep)

        def _muh_dump():
            Path(os.environ["WK_OUT"] + ".muh.json").write_text(
                json.dumps(MUHLOG, ensure_ascii=False, default=str), encoding="utf-8")
        import atexit as _atexit_mh
        _atexit_mh.register(_muh_dump)

    # ── WR (2026-11-04 연구) — WAIT 선택진입 규칙 변형: 체결강도 무시 + 반대 gap <= WK_WR_GAP ──────
    _WR = os.environ.get("WK_WR_GAP", "")
    if _WR:
        from app.trading.macd2 import mu_assist as _mua_wr

        def _wr_dec(cttr_state, g_prev, _lim=float(_WR)):
            opp = None if g_prev is None else max(0.0, -float(g_prev))
            if opp is not None and opp <= _lim:
                return _mua_wr.WAIT_SELECTIVE_APPROVED, opp
            return _mua_wr.WAIT_BLOCK_OPPOSITE_GAP, opp
        monkeypatch.setattr(_mua_wr, "wait_selective_decision", _wr_dec)

    # ── PMSL (2026-11-03 연구) — N1/X2-lite 계열 **오후 손절**만 override ──────────
    # 전역 AFTERNOON_STOP_LOSS 는 건드리지 않는다. worker 의 실포지션 래더 호출이면서
    # N1/X2-lite override(stop_loss_pct_override == X2LITE_MORNING_STOP_LOSS*100)가 넘어온
    # 오후 세션 호출에만 모듈 상수를 그 호출 동안만 바꾼다. shadow_base / premarket_shadow
    # (regime 판정용 섀도우) 호출은 caller 가 worker.py 가 아니므로 그대로다.
    _PMSL = os.environ.get("WK_PMSL", "")
    PMSLLOG = []
    if _PMSL:
        import sys as _sys
        from app.trading.macd2 import time_window_position_manager as _twpm
        _o_ep = _twpm.evaluate_position

        def _pmsl_ep(**kw):
            caller = _sys._getframe(1).f_code.co_filename.replace("\\", "/")
            n1x = kw.get("stop_loss_pct_override")
            hit = (kw.get("session") == "AFTERNOON" and caller.endswith("app/trading/macd2/worker.py")
                   and n1x is not None
                   and abs(float(n1x) - float(config.X2LITE_MORNING_STOP_LOSS) * 100.0) < 1e-9)
            if not hit:
                return _o_ep(**kw)
            old = _twpm.AFTERNOON_STOP_LOSS_PCT
            _twpm.AFTERNOON_STOP_LOSS_PCT = float(_PMSL)
            try:
                dec = _o_ep(**kw)
            finally:
                _twpm.AFTERNOON_STOP_LOSS_PCT = old
            PMSLLOG.append(dict(net=kw.get("net_return_pct"), reason=str(dec.exit_reason), label=dec.label))
            return dec
        monkeypatch.setattr(_twpm, "evaluate_position", _pmsl_ep)

        def _pmsl_dump():
            Path(os.environ["WK_OUT"] + ".pmsl.json").write_text(
                json.dumps(dict(pmsl=_PMSL, log=PMSLLOG), ensure_ascii=False, default=str), encoding="utf-8")
        import atexit as _atexit_pm
        _atexit_pm.register(_pmsl_dump)

    # ── 2026-10-26 MU×체결강도 재생 시계 보정 (정의 변경 아님 — 가시범위만 라이브와 맞춘다) ──
    from app.trading.macd2 import exec_strength as _xs
    from app.trading.macd2 import mu_assist as _mua

    _CTTR_CACHE: dict = {}
    _MU_CACHE: dict = {}

    def _cttr_series(root, day):
        key = (str(root), day)
        if key not in _CTTR_CACHE:
            p = Path(root) / "snapshots" / day / "000660.csv"
            if not p.exists():
                _CTTR_CACHE[key] = None
            else:
                df = pd.read_csv(p, usecols=["ts", "cttr"])
                df["ts"] = pd.to_datetime(df["ts"])
                _CTTR_CACHE[key] = df.dropna(subset=["cttr"]).set_index("ts")["cttr"].sort_index()
        return _CTTR_CACHE[key]

    def _mean_cttr_replay(now, data_dir=None):
        """exec_strength.mean_cttr 와 같은 정의([now-60s, now) 평균), 파일 전체에서 찾는다."""
        root = data_dir if data_dir is not None else config.EXEC_STRENGTH_DATA_DIR
        if not root:
            return None
        end = _xs._naive_kst(now)
        s = _cttr_series(root, end.strftime("%Y-%m-%d"))
        if s is None or s.empty:
            return None
        w = s[(s.index >= end - timedelta(seconds=_xs.WINDOW_SEC)) & (s.index < end)]
        return float(w.mean()) if len(w) else None

    _orig_load_mu_1m = _mua.load_mu_1m

    def _load_mu_1m_replay(now, data_dir=None):
        """그 날 전체가 아니라 now 까지 **끝난** 분봉만 — 라이브와 같은 가시범위."""
        key = (str(data_dir), _mua._naive(now).strftime("%Y-%m-%d"))
        if key not in _MU_CACHE:
            _MU_CACHE[key] = _orig_load_mu_1m(now, data_dir)
        s = _MU_CACHE[key]
        if s is None:
            return None
        return s[s.index + pd.Timedelta(minutes=1) <= _mua._naive(now)]

    monkeypatch.setattr(_xs, "mean_cttr", _mean_cttr_replay)
    monkeypatch.setattr(_mua, "load_mu_1m", _load_mu_1m_replay)
    if _CUKILL:
        monkeypatch.setattr(config, "MU_CATCHUP_KILL", True)

    # ── 변형 C/D (N1 + 청산 단순화) : 하네스 안 monkeypatch 만 ──
    if VAR in ("C", "D", "C0", "D0"):
        def _tp_off(**kw):
            return tpm.PositionManagementDecision(exit_reason=None, sell_fraction=0.0,
                                                  tp1_done=bool(kw.get("tp1_done")),
                                                  peak_net_return=max(0.0, float(kw.get("net_return_pct") or 0.0)),
                                                  label="RESEARCH_TICK_TP_OFF")
        monkeypatch.setattr(tpm, "evaluate_take_profit_immediate", _tp_off)
        _orig_pos = tpm.evaluate_position
        keep_sl = VAR in ("C", "C0")

        def _pos_only_sl(**kw):
            d = _orig_pos(**kw)
            if d.exit_reason == config.EXIT_TW_STOP_LOSS and keep_sl:
                return d
            if d.exit_reason is None:
                return d
            return dataclasses.replace(d, exit_reason=None, sell_fraction=0.0, label="RESEARCH_EXIT_OFF:" + str(d.exit_reason))
        monkeypatch.setattr(tpm, "evaluate_position", _pos_only_sl)
        monkeypatch.setattr(early_take_profit, "is_active", lambda state: False)
        monkeypatch.setattr(worker, "_advance_c1_peak_protection", lambda **kw: None)
        if not keep_sl:
            monkeypatch.setattr(worker.risk_exit, "check_stop_loss", lambda *a, **k: False)
        _orig_gate = time_window_filter.evaluate_time_window_entry

        def _no_reverse(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw):
            dec = _orig_gate(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw)
            pdir = kw.get("position_direction")
            fd = flag_direction if isinstance(flag_direction, Direction) else Direction(str(flag_direction))
            if dec.approved and pdir is not None and pdir != fd and pdir != Direction.HOLD:
                return dataclasses.replace(dec, approved=False, block_reason="RESEARCH_NO_IMMEDIATE_REVERSE",
                                           decision="RESEARCH_NO_IMMEDIATE_REVERSE")
            return dec
        monkeypatch.setattr(time_window_filter, "evaluate_time_window_entry", _no_reverse)
        if VAR in ("C0", "D0"):
            monkeypatch.setattr(config, "H50_ENABLED", False)

    # ── 진입 순간 trend snapshot 기록 (모든 변형, 기록만) + E = P3-TREND-BYPASS ──
    from app.trading.macd2 import n1_adaptive, p3_stack as _p3s, chop_regime as _cr
    ENTRIES = []
    _orig_fin = worker._finalize_tw2_3slot_entry

    def _fin(state, *, outcome, direction, now, session, sizing, presized_chop, bars_3m, signal_detected_at, signal_id):
        snap = n1_adaptive.snapshot(bars_3m, direction)
        rec = dict(t=now.isoformat(), dir=direction.value, sid=signal_id, trend_ok=bool(snap.ok),
                   snap_reason=str(snap.reason), regime=None, bypass=False, simple=False)
        _orig_ner = _p3s.note_entry_regime

        def _ner(st, regime, *, now=None):
            rec["regime"] = regime
            if (VAR == "E" or _TRBP) and regime == _cr.REGIME_CHOP and snap.ok:
                rec["bypass"] = True
                return _orig_ner(st, _cr.REGIME_TREND, now=now)
            r_ = _orig_ner(st, regime, now=now)
            if VAR == "F":
                SIMPLE["epoch"] = int(st.position_epoch or 0) if regime == _cr.REGIME_CHOP else None
                if regime == _cr.REGIME_CHOP:
                    st.p3_position_active = False      # B3/Q2/Y3/H30 스택 미적용 (regime 라벨은 CHOP 그대로)
                    rec["simple"] = True
            return r_
        _p3s.note_entry_regime = _ner
        try:
            return _orig_fin(state, outcome=outcome, direction=direction, now=now, session=session, sizing=sizing,
                             presized_chop=presized_chop, bars_3m=bars_3m, signal_detected_at=signal_detected_at,
                             signal_id=signal_id)
        finally:
            _p3s.note_entry_regime = _orig_ner
            ENTRIES.append(rec)
    monkeypatch.setattr(worker, "_finalize_tw2_3slot_entry", _fin)

    # ── F = P3-CHOP-SIMPLE: 진입 순간 CHOP 각인 포지션에만 단순 관리 ──
    SIMPLE = {"epoch": None}
    STATE = {}

    def _simple():
        st = STATE.get("s")
        return (VAR == "F" and st is not None and st.position is not None and SIMPLE["epoch"] is not None
                and int(st.position_epoch or 0) == SIMPLE["epoch"])
    if VAR == "F":
        from app.trading.macd2 import small_whipsaw_hold as _h50m, risk_exit as _rx
        _o_tp, _o_pos = tpm.evaluate_take_profit_immediate, tpm.evaluate_position

        def _tp(**kw):
            if not _simple():
                return _o_tp(**kw)
            return tpm.PositionManagementDecision(exit_reason=None, sell_fraction=0.0, tp1_done=bool(kw.get("tp1_done")),
                                                  peak_net_return=max(0.0, float(kw.get("net_return_pct") or 0.0)),
                                                  label="SIMPLE_TICK_TP_OFF")

        def _pos(**kw):
            d = _o_pos(**kw)
            if not _simple() or d.exit_reason is None:
                return d
            return dataclasses.replace(d, exit_reason=None, sell_fraction=0.0, label="SIMPLE_EXIT_OFF:" + str(d.exit_reason))
        monkeypatch.setattr(tpm, "evaluate_take_profit_immediate", _tp)
        monkeypatch.setattr(tpm, "evaluate_position", _pos)
        _o_etp = early_take_profit.is_active
        monkeypatch.setattr(early_take_profit, "is_active", lambda state: False if _simple() else _o_etp(state))
        _o_c1 = worker._advance_c1_peak_protection
        monkeypatch.setattr(worker, "_advance_c1_peak_protection", lambda **kw: None if _simple() else _o_c1(**kw))
        _o_sl = _rx.check_stop_loss
        monkeypatch.setattr(_rx, "check_stop_loss", lambda *a, **k: False if _simple() else _o_sl(*a, **k))
        _o_h50 = _h50m.is_active
        monkeypatch.setattr(_h50m, "is_active", lambda state: False if _simple() else _o_h50(state))
        _o_ww = worker._start_whipsaw_watch
        monkeypatch.setattr(worker, "_start_whipsaw_watch", lambda *a, **k: None if _simple() else _o_ww(*a, **k))
        _o_gate = time_window_filter.evaluate_time_window_entry

        def _gate(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw):
            dec = _o_gate(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw)
            pdir = kw.get("position_direction")
            fd = flag_direction if isinstance(flag_direction, Direction) else Direction(str(flag_direction))
            if _simple() and dec.approved and pdir is not None and pdir != fd and pdir != Direction.HOLD:
                return dataclasses.replace(dec, approved=False, block_reason="SIMPLE_NO_IMMEDIATE_REVERSE",
                                           decision="SIMPLE_NO_IMMEDIATE_REVERSE")
            return dec
        monkeypatch.setattr(time_window_filter, "evaluate_time_window_entry", _gate)

    # ── WHIPSAW LOCKOUT (L30 / L45 / ALT3): 신규진입만 30분 차단 ──
    LOCK = {"hist": [], "seen": set(), "until": None, "events": [], "blocked": []}
    if VAR in ("L30", "L45", "ALT3"):
        _o_g = time_window_filter.evaluate_time_window_entry
        _NOT_T3 = set(config.TW_WHIPSAW_REJECT_REASONS)

        def _lock_gate(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw):
            dec = _o_g(bars_3m, flag_direction, flag_bar_dt, decision_at, **kw)
            fd = flag_direction if isinstance(flag_direction, Direction) else Direction(str(flag_direction))
            t = pd.Timestamp(decision_at)
            k = (pd.Timestamp(flag_bar_dt).isoformat(), fd.value)
            if (dec.block_reason or "") not in _NOT_T3 and k not in LOCK["seen"]:
                LOCK["seen"].add(k)
                h = [x for x in LOCK["hist"] if x[0].date() == t.date()] + [(t, 1 if fd == Direction.UP_RED else -1)]
                LOCK["hist"] = h
                if LOCK["until"] is None or t >= LOCK["until"]:
                    flips = [h[j][0] for j in range(1, len(h)) if h[j][1] != h[j - 1][1]]
                    if VAR == "L30":
                        trig = sum(1 for x in flips if x >= t - pd.Timedelta(minutes=30)) >= 2
                    elif VAR == "L45":
                        trig = sum(1 for x in flips if x >= t - pd.Timedelta(minutes=45)) >= 3
                    else:
                        trig = len(h) >= 3 and h[-1][1] == h[-3][1] != h[-2][1]
                    if trig:
                        LOCK["until"] = t + pd.Timedelta(minutes=30)
                        LOCK["events"].append(dict(t=t.isoformat(), until=LOCK["until"].isoformat()))
            if dec.approved and LOCK["until"] is not None and t < LOCK["until"]:
                LOCK["blocked"].append(dict(t=t.isoformat(), dir=fd.value, held=str(kw.get("position_direction"))))
                return dataclasses.replace(dec, approved=False, block_reason="WHIPSAW_LOCKOUT", decision="WHIPSAW_LOCKOUT")
            return dec
        monkeypatch.setattr(time_window_filter, "evaluate_time_window_entry", _lock_gate)

    base = pickle.load(open(SP / "base_d83.pkl", "rb"))["trades"]
    rows = []
    for t in base:
        if t["date"] >= P:
            continue
        rows.append(chop_regime.ShadowTrade(
            shadow_trade_id=chop_regime.make_shadow_trade_id(t["entry_time"], t["direction"], t.get("slot_number") or 1),
            trading_date=t["date"], entry_time=pd.Timestamp(t["entry_time"]).isoformat(),
            exit_time=pd.Timestamp(t["exit_time"]).isoformat(), direction=t["direction"],
            slot=int(t.get("slot_number") or 1), entry_price=float(t["entry_price"]),
            exit_price=float(t["exit_price"]), net_pct=float(t["net_pct"]),
            h50_intervened=bool(t["h50_held"]), tp1_hit=bool(t["tp1_hit"])))
    chop_regime.save_ledger(rows, source="replay-seed")

    def rd(n):
        x = pd.read_csv(SP / "data" / n)
        x["datetime"] = pd.to_datetime(x["datetime"].astype(str).str[:19])
        return x
    hy = pd.concat([rd(f"replay_{x}_hynix_1m.csv") for x in (P2, P, D)]).drop_duplicates("datetime", keep="last")
    hy["datetime"] = hy["datetime"].dt.tz_localize(KST)
    hy = hy.sort_values("datetime").reset_index(drop=True)
    etf = {}
    for sym, tag in ((config.LONG_SYMBOL, "long"), (config.INVERSE_SYMBOL, "inverse")):
        d = pd.concat([rd(f"replay_{x}_{tag}_1m.csv") for x in (P, D)]).drop_duplicates("datetime", keep="last")
        d["datetime"] = d["datetime"].dt.tz_localize(KST)
        etf[sym] = d.sort_values("datetime").reset_index(drop=True)
    etf[config.WATCH_SYMBOL] = hy[hy["datetime"] >= pd.Timestamp(_d(P).replace(hour=8))].reset_index(drop=True)
    idx = {s: e.set_index("datetime") for s, e in etf.items()}

    svc = MarketDataService(mode="mock", fetch_minute_candles=lambda *a, **k: (pd.DataFrame(columns=hy.columns), {}),
                            fetch_quote=lambda mode, symbol: (None, None))
    clock = {"now": _d(P).replace(hour=9)}

    def _px_raw(sym, now):
        if _TICKPX:
            _r = _tick_row(sym, now)
            if _r is not None:
                return float(_r["price"])
        d = etf[sym]
        ts = pd.Timestamp(now)
        if FILL == "REAL":
            m = ts.floor("min")
            ix = idx[sym]
            if m in ix.index:
                r = ix.loc[m]
                if isinstance(r, pd.DataFrame):
                    r = r.iloc[-1]
                frac = (ts - m).total_seconds() / 60.0
                p = float(r["open"]) + (float(r["close"]) - float(r["open"])) * frac
                tick = ETF_TICK if sym != config.WATCH_SYMBOL else 1000.0
                return round(p / tick) * tick
        done = d[d["datetime"] + pd.Timedelta(minutes=1) <= ts]
        return float(done["close"].iloc[-1]) if len(done) else None

    _pxc = {"t": None, "v": {}}

    def px(sym, now):
        if _pxc["t"] != now:
            _pxc["t"], _pxc["v"] = now, {}
        if sym not in _pxc["v"]:
            _pxc["v"][sym] = _px_raw(sym, now)
        return _pxc["v"][sym]

    svc.get_quote = lambda symbol: (None if px(symbol, clock["now"]) is None else QuoteSnapshot(
        symbol=symbol, price=px(symbol, clock["now"]), fetched_at=clock["now"], age_sec=0.0, source="replay", error=None))
    svc.refresh_quotes = lambda *a, **k: None
    def _book(sym, now):
        """(bid1, ask1). 1초 스냅샷이 있으면 실제 최우선호가, 없으면 기존 모형(시세, 시세+5)."""
        if _TICKPX:
            r = _tick_row(sym, now)
            if r is not None and pd.notna(r.get("best_bid")) and pd.notna(r.get("best_ask")):
                b, a = float(r["best_bid"]), float(r["best_ask"])
                if b > 0 and a > 0:
                    return b, a
        p = px(sym, now)
        return (None, None) if p is None else (float(p), float(p) + ETF_TICK)

    BOOKQ = {}

    def _set_book(sym, now):
        b, a = _book(sym, now)
        if b is not None:
            BOOKQ[sym] = (b, a)
        return b

    # ── F = RSBRK : RS 상위20% x1.25 증액  +  (오전 ∧ C1 chop) 돌파확인 지연 ──
    #   지연 대상: 12시 이전 ∧ PE<0.20 ∧ 최근30분 range<1.0% ∧ EMA20 교차>=3
    #   지연 규칙: T+3 승인 시 매수하지 않고 (플래그봉, 확정봉) 극값 ±1틱을 trigger 로 걸어
    #             30분 안에 돌파하면 그 시점에 production 경로로 진입, 미도달이면 폐기.
    #   그 외 플래그는 현행 그대로 진입하되 RS 상위20% 면 비중만 x1.25.
    RSBRK = False   # production E 를 그대로 검증한다 (연구 흉내 OFF)
    RS_UP, WAIT_MIN, BUF_TICKS, HY_TICK = 1.25, 15, 0.0, 500.0
    PE_MAX, RNG_MAX, XC_MIN, AM_HOUR = 0.20, 1.0, 3, 12
    RS_TBL = {}
    _SGN = {"atr60": 1.0, "min_open": -1.0, "xc30": 1.0}
    PEND = {"rec": None}
    FIRE = {"on": None}
    REC = {"on": False, "cache": {}, "ctr": {}}
    CTX = {"bars_3m": None, "flag_bar_ts": None}
    ARMQ = {"exit": None}
    LOG = []
    _CACHE_MAX = {"evaluate_entry_chop": 1}

    def _memo(mod, name):
        orig = getattr(mod, name)

        def w(*a, **k):
            if FIRE["on"] is not None:
                i = REC["ctr"].get(name, 0)
                REC["ctr"][name] = i + 1
                lst = REC["cache"].get(name) or []
                return lst[i] if i < len(lst) else orig(*a, **k)
            r = orig(*a, **k)
            if REC["on"]:
                lst = REC["cache"].setdefault(name, [])
                if len(lst) < _CACHE_MAX.get(name, 99):
                    lst.append(r)
            return r
        monkeypatch.setattr(mod, name, w)

    if RSBRK:
        from app.trading.macd2 import teg_gate as _teg, time_window_3slot as _t3s
        from app.trading.macd2 import small_whipsaw_hold as _h50b
        for _m, _n in ((time_window_filter, "evaluate_time_window_entry"),
                       (time_window_filter, "evaluate_tw2_extra_vetoes"),
                       (_t3s, "resolve_slot"), (_t3s, "evaluate_trend_quality"),
                       (_t3s, "evaluate_slot1_chop_veto"), (_t3s, "evaluate_afternoon_reentry"),
                       (_teg, "evaluate_teg"), (early_take_profit, "evaluate_entry_chop")):
            _memo(_m, _n)
        _o_shadow = worker._p3_note_shadow_flag
        monkeypatch.setattr(worker, "_p3_note_shadow_flag",
                            lambda **kw: None if FIRE["on"] is not None else _o_shadow(**kw))
        _o_h50act = _h50b.is_active
        monkeypatch.setattr(_h50b, "is_active",
                            lambda state: False if FIRE["on"] is not None else _o_h50act(state))

    def _win(now, same_day=True):
        ts = pd.Timestamp(now)
        d = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= ts]
        if same_day:
            d = d[d["datetime"].dt.date == ts.date()]
        return d

    EP = {"imm": 0, "wait": 0}

    def _macd_gap_series(bars_3m):
        c = pd.Series(bars_3m["close"].astype(float).to_numpy())
        m = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
        return (m - m.ewm(span=9, adjust=False).mean()).to_numpy()

    def _early_pass(now, direction, macd_snap):
        """T+3 승인 직후 '이미 돌파 임박 + 되돌림 없음 + gap 확대' 이면 즉시 진입."""
        bars = CTX["bars_3m"]
        if bars is None or not len(bars):
            return False, None
        flag_dt = worker._parse_iso_dt(CTX["flag_bar_ts"])
        up = (direction == Direction.UP_RED)
        trig = _trigger_px(bars, flag_dt, macd_snap.bar_dt, direction)
        px = _hy_px(now)
        if trig is None or px is None:
            return False, None
        # ① 현재가가 플래그 방향 극값의 0.2% 이내 (이미 넘었으면 거리가 음수 -> 통과)
        dist = ((trig - px) if up else (px - trig)) / px * 100.0
        # UP 만 거리기준 완화(0.2% -> 0.3%). DOWN 은 불변. c2/c3 는 양쪽 다 그대로 요구한다.
        c1 = dist <= (0.3 if up else 0.2)
        # ② 확정봉이 플래그 방향과 같거나 중립
        ts = pd.to_datetime(bars["datetime"])
        cb = bars[ts == pd.Timestamp(macd_snap.bar_dt)]
        if len(cb):
            r = cb.iloc[-1]
            body = (float(r["close"]) - float(r["open"])) / float(r["open"]) * 100.0
            c2 = (body >= 0) if up else (body <= 0)
        else:
            c2 = False
        # ③ MACD gap 이 플래그 이후 계속 확대
        g = _macd_gap_series(bars)
        fb = bars[ts == pd.Timestamp(flag_dt)] if flag_dt is not None else None
        if fb is not None and len(fb) and len(g) >= 2:
            fi = int(fb.index[-1]) if fb.index[-1] < len(g) else len(g) - 2
            c3 = abs(float(g[-1])) > abs(float(g[fi]))
        else:
            c3 = False
        return bool(c1 and c2 and c3), dict(dist=round(float(dist), 4), c1=bool(c1), c2=bool(c2),
                                             c3=bool(c3), up=bool(up),
                                             upfast=bool(up and c2 and c3 and 0.2 < dist <= 0.3))

    def _am_chop(now):
        """BREAKOUT15 전면 적용 — early-pass 판정은 _eow 에서 따로 한다."""
        return True

    def _am_chop_unused(now):
        ts = pd.Timestamp(now)
        if ts.hour >= AM_HOUR:
            return False
        d = _win(now)
        if len(d) < 30:
            return False
        ca = d["close"].astype(float)
        ema = ca.ewm(span=20, adjust=False).mean()
        w = d.iloc[-30:]
        c = w["close"].to_numpy(float)
        path = float(np.abs(np.diff(c)).sum())
        if path <= 0:
            return False
        pe = abs(c[-1] - c[0]) / path
        rng = float((w["high"].max() - w["low"].min()) / c[-1] * 100.0)
        s = np.sign((ca - ema).to_numpy())[-30:]
        s = s[s != 0]
        xc = int((np.diff(s) != 0).sum()) if len(s) > 1 else 0
        return pe < PE_MAX and rng < RNG_MAX and xc >= XC_MIN

    def _rs_hit(now):
        t = RS_TBL.get(D)
        if t is None:
            return False
        ts = pd.Timestamp(now)
        done = _win(now, same_day=False)
        today = done[done["datetime"].dt.date == ts.date()]
        if len(done) < 120 or len(today) < 30:
            return False
        c = done["close"].to_numpy(float)
        e20 = done["close"].astype(float).ewm(span=20, adjust=False).mean().to_numpy()
        s = np.sign(c - e20)[-30:]
        s = s[s != 0]
        f = dict(atr60=float(np.abs(np.diff(c[-60:])).mean()) / c[-1] * 100,
                 min_open=float(ts.hour * 60 + ts.minute - 540),
                 xc30=float(int((np.diff(s) != 0).sum()) if len(s) > 1 else 0))
        return sum(_SGN[k] * (f[k] - t["mu"][k]) / t["sd"][k] for k in _SGN) >= t["thr"]

    def _hy_px(now):
        if _TICKPX:
            _r = _tick_row(config.WATCH_SYMBOL, now)
            if _r is not None:
                return float(_r["price"])
        ix = idx.get(config.WATCH_SYMBOL)
        ts = pd.Timestamp(now)
        m = ts.floor("min")
        if ix is not None and m in ix.index:
            r = ix.loc[m]
            if isinstance(r, pd.DataFrame):
                r = r.iloc[-1]
            return float(r["open"]) + (float(r["close"]) - float(r["open"])) * ((ts - m).total_seconds() / 60.0)
        d = etf.get(config.WATCH_SYMBOL)
        done = d[d["datetime"] + pd.Timedelta(minutes=1) <= ts] if d is not None else None
        return float(done["close"].iloc[-1]) if done is not None and len(done) else None

    def _trigger_px(bars_3m, flag_bar_dt, confirm_bar_dt, direction):
        ts = pd.to_datetime(bars_3m["datetime"])
        rows = []
        for dt in (flag_bar_dt, confirm_bar_dt):
            if dt is None:
                continue
            mm = bars_3m[ts == pd.Timestamp(dt)]
            if len(mm):
                rows.append(mm.iloc[-1])
        if not rows:
            return None
        if direction == Direction.UP_RED:
            return max(float(r["high"]) for r in rows) + BUF_TICKS * HY_TICK
        return min(float(r["low"]) for r in rows) - BUF_TICKS * HY_TICK

    def _pub(rec, **extra):
        return dict(sid=rec["signal_id"], dir=rec["dir"], trigger=rec["trigger"],
                    armed_at=pd.Timestamp(rec["armed_at"]).isoformat(),
                    deadline=pd.Timestamp(rec["deadline"]).isoformat(), **extra)

    if RSBRK:
        _orig_resolve = worker._resolve_tw2_3slot_candidate
        _body = worker._resolve_tw2_3slot_candidate_body
        _orig_eow = worker._execute_or_wait

        def _rs_eow(*, sid, is3, budget_multiplier=1.0, **kw):
            """RS 상위20% 증액 — 즉시진입/대기체결 두 경로 모두에서 동일하게 적용.
            일 한도(예산 x3.0) 안에서만 올리고, 증액분은 누계에 직접 반영한다.
            production note_entry 는 '증액 전' 배수만 기록하므로 반영하지 않으면
            뒤 거래가 여유를 과대평가해 3,000만원을 넘는다(2026-10-05 실측).
            """
            bm = float(budget_multiplier or 1.0)
            now, st = kw["now"], kw["state"]
            _extra = 0.0
            if (is3 or sid.endswith(":BRK")) and _rs_hit(now):
                from app.trading.macd2 import position_sizing as _ps
                _room = float(config.X2LITE_SIZING_DAILY_EXPOSURE_CAP) - float(_ps.exposure_used(st))
                _base, _want = bm, bm * RS_UP
                bm = min(_want, max(_room, 0.0))
                _extra = max(bm - _base, 0.0)
                LOG.append(dict(ev="RS_UP", t=now.isoformat(), sid=sid, want=round(_want, 4),
                                room=round(_room, 4), applied=round(bm, 4), extra=round(_extra, 4),
                                capped=bool(_want > _room + 1e-9), armed_at=now.isoformat()))
            _out = _orig_eow(budget_multiplier=bm, **kw)
            if _extra > 0 and _out is not None and _out.final_state == worker.SignalState.EXECUTED:
                st.x2lite_exposure_used_today = round(
                    float(getattr(st, "x2lite_exposure_used_today", 0.0) or 0.0) + _extra, 6)
            return _out

        def _eow(*, broker, market_data, state, now, macd_snap, direction, signal_id, signal_type,
                 position, result, signal_detected_at=None, budget_multiplier=1.0):
            sid = str(signal_id)
            is3 = sid.endswith(":TW2_3SLOT_CONFIRM")
            bm = float(budget_multiplier or 1.0)
            if is3 and FIRE["on"] is None:
                _ok, _d = _early_pass(now, direction, macd_snap)
                if _ok:
                    EP["imm"] += 1
                    LOG.append(dict(ev="EARLY_PASS", t=now.isoformat(), sid=sid, dir=direction.value,
                                    armed_at=now.isoformat(), **(_d or {})))
                    return _rs_eow(broker=broker, market_data=market_data, state=state, now=now,
                                   macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                                   signal_type=signal_type, position=position, result=result,
                                   signal_detected_at=signal_detected_at, budget_multiplier=bm,
                                   sid=sid, is3=is3)
                EP["wait"] += 1
                if _d:
                    LOG.append(dict(ev="EARLY_FAIL", t=now.isoformat(), sid=sid, dir=direction.value,
                                    armed_at=now.isoformat(), **_d))
            if is3 and FIRE["on"] is None and _am_chop(now):
                flag_bar_dt = worker._parse_iso_dt(CTX["flag_bar_ts"])
                trig = _trigger_px(CTX["bars_3m"], flag_bar_dt, macd_snap.bar_dt, direction)
                if trig is not None:
                    rec = dict(direction=direction, dir=direction.value, signal_id=sid,
                               flag_bar_dt=flag_bar_dt, trigger=trig, armed_at=now,
                               deadline=now + timedelta(minutes=WAIT_MIN),
                               cache={k: list(v) for k, v in REC["cache"].items()})
                    if PEND["rec"] is not None:
                        LOG.append(_pub(PEND["rec"], ev="SUPERSEDED", at=now.isoformat()))
                    PEND["rec"] = rec
                    LOG.append(_pub(rec, ev="ARMED_BRK15",
                                    held=(position.symbol if position is not None else None)))
                    tgt = worker.order_executor.target_symbol_for_direction(direction)
                    if position is not None and position.quantity > 0 and position.symbol != tgt:
                        ARMQ["exit"] = dict(macd_snap=macd_snap, direction=direction,
                                            position=position, signal_id=sid)
                    else:
                        state.processed_signal_ids = list(state.processed_signal_ids) + [sid]
                    return None
            return _rs_eow(broker=broker, market_data=market_data, state=state, now=now,
                           macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                           signal_type=signal_type, position=position, result=result,
                           signal_detected_at=signal_detected_at, budget_multiplier=bm,
                           sid=sid, is3=is3)

        def _resolve_rb(*, broker, market_data, state, now, macd_snap, bars_3m, df_1m, position, result):
            rec = PEND["rec"]
            if rec is not None:
                if now.date() != pd.Timestamp(rec["armed_at"]).date() or now > rec["deadline"]:
                    PEND["rec"] = None
                    LOG.append(_pub(rec, ev="EXPIRED", at=now.isoformat()))
                    rec = None
            if rec is not None:
                p = _hy_px(now)
                hit = p is not None and ((p >= rec["trigger"]) if rec["dir"] == "UP_RED" else (p <= rec["trigger"]))
                if hit:
                    PEND["rec"] = None
                    FIRE["on"] = rec
                    REC["ctr"] = {}
                    REC["cache"] = rec["cache"]
                    try:
                        out = _body(broker=broker, market_data=market_data, state=state, now=now,
                                    macd_snap=macd_snap, bars_3m=bars_3m, df_1m=df_1m,
                                    position=position, result=result, direction=rec["direction"],
                                    signal_id=rec["signal_id"] + ":BRK", flag_bar_dt=rec["flag_bar_dt"])
                    finally:
                        FIRE["on"] = None
                    ok = bool(out is not None and out.final_state == worker.SignalState.EXECUTED)
                    LOG.append(_pub(rec, ev="FIRED", at=now.isoformat(), price=p, executed=ok,
                                    delay_min=(now - pd.Timestamp(rec["armed_at"]).to_pydatetime()).total_seconds() / 60.0))
                    return out if ok else None
            CTX["bars_3m"] = bars_3m
            CTX["flag_bar_ts"] = state.tw2_3slot_pending_flag_bar_ts
            REC["cache"] = {}
            REC["on"] = True
            ARMQ["exit"] = None
            try:
                out = _orig_resolve(broker=broker, market_data=market_data, state=state, now=now,
                                    macd_snap=macd_snap, bars_3m=bars_3m, df_1m=df_1m,
                                    position=position, result=result)
            finally:
                REC["on"] = False
            q = ARMQ["exit"]
            ARMQ["exit"] = None
            if q is not None:
                dec = worker.MajorFlagDecision(
                    approved=False, score=0.0, required_score=0.0, decision="AMCHOP_BREAKOUT_PENDING",
                    reasons=("am chop breakout pending",), component_scores={}, metrics={},
                    is_reversal=True, fast_reversal=False, block_reason="AMCHOP_BREAKOUT_PENDING")
                ex_out = worker._execute_reversal_exit_only_for_filtered_entry(
                    broker=broker, state=state, macd_snap=q["macd_snap"], direction=q["direction"],
                    position=q["position"], decision=dec, result=result, gate_mode="TW2_3SLOT",
                    signal_id_override=q["signal_id"])
                if ex_out is not None:
                    worker._apply_exit_outcome(state, ex_out)
                    return ex_out
            return out
        monkeypatch.setattr(worker, "_execute_or_wait", _eow)
        monkeypatch.setattr(worker, "_resolve_tw2_3slot_candidate", _resolve_rb)

    Broker = RealFillBroker if FILL == "REAL" else FakeBroker
    broker = Broker(cash=50_000_000.0, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 6_000.0})
    broker.book = BOOKQ
    if _TICKPX:
        _o_sell_mkt = broker.sell_market

        def _sell_mkt(symbol, qty, client_order_id):
            """매도 체결가 = 실제 최우선 매수호가(bid1). 없으면 기존 모형(시세)."""
            bk = BOOKQ.get(symbol)
            if bk and bk[0]:
                broker.set_quote(symbol, float(bk[0]))
            return _o_sell_mkt(symbol, qty, client_order_id)
        broker.sell_market = _sell_mkt
    state = state_store.default_state()
    state.auto_trade_on = True
    state.stopped = False      # service.start() 와 같은 상태 (E hard safety 가 stopped 를 본다)
    state.mode = "mock"
    state.budget = 10_000_000.0
    # ── 2026-10-12 v4: 기록 전용 wrapper (원 함수를 그대로 호출 — 동작 불변) ──
    from app.trading.macd2 import chop_defense as _cd
    PRODLOG = []
    _o_b3 = _cd.b3_stop_action
    def _b3_rec(st_, **kw):
        res = _o_b3(st_, **kw)
        ea = kw.get("entry_at")
        PRODLOG.append(dict(k="B3STOP", t=kw["now"].isoformat(), entry=ea.isoformat() if ea else None,
                            net=float(kw.get("net_return_pct") or 0.0), res=res, active=bool(_cd.is_active(st_))))
        return res
    monkeypatch.setattr(_cd, "b3_stop_action", _b3_rec)
    _o_mc = _cd.morning_no_trade
    def _mc_rec(st_, now, df_1m, *a, **kw):
        res = _o_mc(st_, now, df_1m, *a, **kw)
        if res:
            PRODLOG.append(dict(k="MC", t=now.isoformat(), on_at=st_.chop_mc_on_at))
        return res
    monkeypatch.setattr(_cd, "morning_no_trade", _mc_rec)
    _o_slow = worker._slow_tp2_ceiling
    def _slow_rec(st_, tp2, *, now):
        res = _o_slow(st_, tp2, now=now)
        if float(res) != float(tp2):
            PRODLOG.append(dict(k="SLOWTP2", t=now.isoformat() if now else None,
                                entry=getattr(st_, "last_time_window_entry_at", None),
                                tp2_from=float(tp2), tp2_to=float(res)))
        return res
    monkeypatch.setattr(worker, "_slow_tp2_ceiling", _slow_rec)
    _o_fhs = worker._e_fire_hard_safety
    def _fhs_rec(st_, now, rec):
        res = _o_fhs(st_, now, rec)
        if res:
            PRODLOG.append(dict(k="E_FIRE_BLOCK", t=now.isoformat(), res=res))
        return res
    monkeypatch.setattr(worker, "_e_fire_hard_safety", _fhs_rec)
    if os.environ.get("WK_MC_BLOCK_EXIT") == "1":
        # 연구식 MC: MC 가 막은 반대 플래그의 **청산까지** 막는다 (비교 전용, production 아님)
        _o_rx = worker._execute_reversal_exit_only_for_filtered_entry
        def _rx(**kw):
            dec = kw.get("decision")
            if dec is not None and (dec.block_reason or "") == config.CHOP_MC_BLOCK_REASON:
                PRODLOG.append(dict(k="MC_EXIT_BLOCKED", t=clock["now"].isoformat()))
                return None
            return _o_rx(**kw)
        monkeypatch.setattr(worker, "_execute_reversal_exit_only_for_filtered_entry", _rx)
    # ── 2026-10-13 v6 research: 오전 2번째 진입에도 기존 trend quality 적용 (WK_Q2=B 현행기준 / C 완화 2/5) ──
    _Q2 = os.environ.get("WK_Q2", "")
    Q2LOG = []
    if _Q2:
        from app.trading.macd2 import time_window_3slot as _t3
        _o_rs = _t3.resolve_slot
        _q2s = {"slot2": False}
        def _rs(**kw):
            dec = _o_rs(**kw)
            _q2s["slot2"] = False
            if dec.slot_allowed and dec.session == _t3.SESSION_MORNING and int(kw.get("morning_count") or 0) == 1:
                _q2s["slot2"] = True
                dec = dataclasses.replace(dec, requires_quality_gate=True)
            return dec
        monkeypatch.setattr(_t3, "resolve_slot", _rs)
        _o_q = _t3.evaluate_trend_quality
        def _q(*a, **k):
            if _q2s["slot2"] and _Q2 == "C":
                k["required"] = 2
            res = _o_q(*a, **k)
            if _q2s["slot2"]:
                Q2LOG.append(dict(passed=int(res.passed_count), approved=bool(res.approved)))
            return res
        monkeypatch.setattr(_t3, "evaluate_trend_quality", _q)
    # ── 2026-10-13 v6 research: 반대 플래그 quality < WK_OPPQ/5 이면 기존 포지션 유지(청산·스위치 안 함) ──
    _OPPQ = int(os.environ.get("WK_OPPQ", "0") or 0)
    OPPQLOG = []
    if _OPPQ:
        from app.trading.macd2 import time_window_3slot as _t3o
        _o_body = worker._resolve_tw2_3slot_candidate_body
        def _body(**kw):
            pos_dir = worker._position_direction(kw.get("position"))
            if pos_dir is not None and pos_dir != kw["direction"]:
                q = _t3o.evaluate_trend_quality(kw["bars_3m"], kw["direction"])
                held = int(q.passed_count) < _OPPQ
                OPPQLOG.append(dict(t=kw["now"].isoformat(), sid=kw["signal_id"], passed=int(q.passed_count), held=held))
                if held:
                    kw["state"].processed_signal_ids.append(kw["signal_id"])
                    return None
            return _o_body(**kw)
        monkeypatch.setattr(worker, "_resolve_tw2_3slot_candidate_body", _body)
    if _MODE:
        # ── 2026-10-30 ARM3: 전략모드·MU 토글을 env 로 직접 고른다 ──────────
        _MODE_MAP = {"CHOP": strategy_mode.MODE_CHOP, "E": strategy_mode.MODE_E,
                     "P3": strategy_mode.MODE_P3, "N1": strategy_mode.MODE_N1}
        if _MODE in ("CHOP", "E"):
            from app.trading.macd2 import e_strategy as _es
            monkeypatch.setattr(config, "E_RS_TABLE_PATH", os.environ["RS_TABLE"])
            if hasattr(_es._table_from_path, "_cache"):
                del _es._table_from_path._cache
            if os.environ.get("WK_WATCH_RAW", "1") == "1":
                monkeypatch.setattr(worker, "_e_watch_price", lambda market_data, df_1m: _hy_px(clock["now"]))
        strategy_mode.apply(state, _MODE_MAP[_MODE])
        # 현행 운영 기준선 = ECHOPN (고정 ±1.5% 청산 OFF). env 로만 되돌린다.
        state.fixed_exit_15_enabled = os.environ.get("WK_FIX15", "0") == "1"
        state.mu_assist_enabled = bool(_MUA)
        state.mu_assist_xs_enabled = bool(_XS)
    elif VAR in ("EPROD", "ECHOP", "ECHOPB", "ECHOPC", "PAR", "PAR70", "PAR50"):
        from app.trading.macd2 import e_strategy as _es
        monkeypatch.setattr(config, "E_RS_TABLE_PATH", os.environ["RS_TABLE"])
        if hasattr(_es._table_from_path, "_cache"):
            del _es._table_from_path._cache
        if os.environ.get("WK_WATCH_RAW", "1") == "1":
            monkeypatch.setattr(worker, "_e_watch_price", lambda market_data, df_1m: _hy_px(clock["now"]))
        strategy_mode.apply(state, strategy_mode.MODE_CHOP
                            if VAR in ("ECHOP", "ECHOPB", "ECHOPC", "PAR", "PAR70", "PAR50") else strategy_mode.MODE_E)
        # ── 2026-10-20 H50/SMALL_WHIPSAW 연구 변형 (하네스 전용 monkeypatch) ──
        #   A = ECHOP  현행 그대로
        #   B = ECHOPB CHOP 에서 H50 HOLD 만 OFF
        #   C = ECHOPC HOLD 조건 약화(발동 어렵게 + 더 빨리 해제)
        # production 상수는 건드리지 않는다 -- monkeypatch 범위 안에서만 바뀐다.
        # 오늘 운영과 동일하게 고정 ±1.5% 청산을 켠다 (CHOP + FIXED_EXIT15).
        # 이게 꺼져 있으면 B3_SL 이 먼저 터져 반대플래그 HOLD 판정까지
        # 가지도 못한다 -- 2026-10-20 재생에서 실제로 그랬다.
        state.fixed_exit_15_enabled = VAR not in ("PAR", "PAR70", "PAR50")
        if VAR == "ECHOPB":
            monkeypatch.setattr(config, "H50_ENABLED", False)
        elif VAR == "ECHOPC":
            monkeypatch.setattr(config, "H50_RANGE_MAX_PCT", 1.20)   # 2.35 -> 1.20
            monkeypatch.setattr(config, "H50_TREND_BREAK_BARS", 1)   # 2 -> 1 (조기 해제)
            monkeypatch.setattr(config, "H50_MAX_HOLD_MIN", 20)      # 60 -> 20
    else:
        strategy_mode.apply(state, strategy_mode.MODE_P3 if VAR in ("A", "E", "F", "L30", "L45", "ALT3", "UPFASTRS") else strategy_mode.MODE_N1)
    STATE["s"] = state

    # ── 2026-10-30 ARM3 ① 진입 게이트 전수 추적 (기록 전용, 판정 무영향) ──────
    GATELOG = []
    from app.trading.macd2 import exec_strength as _xsm
    from app.trading.macd2 import mu_assist as _muam
    from app.trading.macd2 import mu_catchup as _mucu
    from app.trading.macd2 import teg_gate as _tegm
    from app.trading.macd2 import time_window_3slot as _t3m
    from app.trading.macd2 import time_window_filter as _twfm

    def _jsonable(v):
        if isinstance(v, (int, float, bool, str, type(None))):
            return v
        if isinstance(v, (list, tuple, set, frozenset)):
            return [_jsonable(x) for x in v]
        if isinstance(v, dict):
            return {str(k): _jsonable(x) for k, x in v.items()}
        return str(v)

    def _glog(ev, **kw):
        GATELOG.append(dict(ev=ev, t=clock["now"].isoformat(), **{k: _jsonable(v) for k, v in kw.items()}))

    def _wrap(mod, name, rec):
        orig = getattr(mod, name)

        def w(*a, **k):
            r = orig(*a, **k)
            try:
                rec(r, a, k)
            except Exception:
                pass
            return r
        monkeypatch.setattr(mod, name, w)

    _wrap(_twfm, "evaluate_time_window_entry", lambda r, a, k: _glog(
        "TW", dir=getattr(a[1] if len(a) > 1 else k.get("flag_direction"), "value", None),
        flag_bar=str(a[2] if len(a) > 2 else k.get("flag_bar_dt")), approved=bool(r.approved),
        decision=r.decision, block=r.block_reason, skip_conf=bool(k.get("skip_confirmation_check")),
        metrics=dict(r.metrics or {})))
    _wrap(_twfm, "evaluate_tw2_extra_vetoes", lambda r, a, k: _glog(
        "TW2VETO", dir=getattr(a[1] if len(a) > 1 else None, "value", None), vetoed=bool(r[0]), reason=r[1]))
    _wrap(_tegm, "evaluate_teg", lambda r, a, k: _glog(
        "TEG", dir=getattr(a[1] if len(a) > 1 else None, "value", None),
        approved=bool(r.approved), reasons=list(r.reject_reasons or [])))
    _wrap(_t3m, "resolve_slot", lambda r, a, k: _glog(
        "SLOT", dir=getattr(k.get("direction"), "value", None), allowed=bool(r.slot_allowed),
        slot=r.slot_number, session=r.session, reject=r.reject_reason,
        need_quality=bool(r.requires_quality_gate), need_teg=bool(r.requires_teg_gate),
        used=k.get("slots_used_today"), am=k.get("morning_count"), pm=k.get("afternoon_count"),
        cap=k.get("daily_cap")))
    _wrap(_t3m, "evaluate_trend_quality", lambda r, a, k: _glog(
        "QUALITY", passed=int(r.passed_count), approved=bool(r.approved)))
    _wrap(_xsm, "evaluate", lambda r, a, k: _glog(
        "XSTRENGTH", dir=getattr(a[0] if a else None, "value", None), state=r.get("state"), cttr=r.get("cttr")))
    _wrap(_muam, "entry_status_for", lambda r, a, k: _glog("MU_ENTRY_STATUS", sid=a[0] if a else None, status=r))
    _wrap(_muam, "entry_veto_reason", lambda r, a, k: _glog("MU_ENTRY_VETO", sid=a[0] if a else None, veto=r))
    _wrap(_mucu, "evaluate", lambda r, a, k: _glog("MU_CATCHUP", res=r))
    _wrap(worker, "_mu_xs_strong", lambda r, a, k: _glog(
        "XS_STRONG", sid=(a[1] if len(a) > 1 else None), strong=bool(r), cttr=(r or {}).get("cttr")))
    _wrap(worker, "_mu_xs_teg_override", lambda r, a, k: _glog("XS_TEG_OVERRIDE", applied=bool(r)))
    def _rec_decision(r, a, k):
        """T+3 판정 기록 + 신호 ⑥(반대 플래그 T+3 확정 전) 상태 갱신.

        `TIME_WINDOW_PENDING_CONFIRMATION` 이면 그 플래그는 아직 T+3 확정 전이다.
        같은 signal_id 로 그 밖의 판정이 오면(승인/거절) 확정된 것이므로 지운다."""
        dec, sid = a[1], (a[2] if len(a) > 2 else "")
        _glog("DECISION", sid=sid, approved=bool(dec.approved), decision=dec.decision,
              block=dec.block_reason, metrics=dict(dec.metrics or {}))
        base = str(sid or "").split(":")[0]
        if not base:
            return
        d = "UP_RED" if base.endswith("UP_RED") else "DOWN_BLUE"
        if str(dec.block_reason or "") == config.TW_PENDING_CONFIRMATION:
            OPP_PENDING[base] = d
        else:
            OPP_PENDING.pop(base, None)
        if bool(dec.approved):
            OPP_APPROVED[base] = d
    _wrap(worker, "_persist_tw2_3slot_decision", _rec_decision)

    # ── ARM3 ② +3% 틱 수익보호선 ──────────────────────────────────────────
    #   SOFT(B)/MEDIUM(C) : 2026-10-30. +3% 도달 **즉시** 보호 활성.
    #   W1 / W2           : 2026-10-31 후속. +3% 도달은 **ARM 후보**일 뿐이고,
    #                       추세약화 신호가 1개(W1) / 2개(W2) 확인될 때만 보호를 켠다.
    # 기존 청산(TP/SL/C1/B3/H50/반대신호/시간/15:00)은 한 줄도 바꾸지 않는다.
    # 같은 tick 에 기존 청산이 발동하면 그쪽이 이긴다 — 이 블록은 기존 위험관리가
    # 아무것도 하지 않은 tick 에서만 판정한다("먼저 발생한 것 적용").
    ARM = {"epoch": None, "peak": 0.0, "line": None, "prev_t": None,
           "cand": False, "active": False, "mu_confirm": False, "sigs": ()}
    ARM3LOG = []
    ARM_REASON = "TICK_ARM3_STOP"
    #: 반대방향 플래그가 T+3 확정 전인지 — 게이트 추적 wrapper 가 갱신한다(신호 ⑥).
    OPP_PENDING = {}
    #: T+3 확정 + 진입게이트까지 **통과한** 플래그 (청산 유예의 강한 붕괴 ④).
    OPP_APPROVED = {}

    if _ARM in _ARM_LADDER:
        from app.trading.macd2 import major_flag_filter as _mff
        from app.trading.macd2 import mu_assist as _muw
        from app.trading.macd2 import order_executor as _oex
        from app.trading.macd2 import signal_engine as _sew
        from app.trading.macd2 import time_window_filter as _twfw
        from app.trading.macd2.models import Direction as _Dir
        from app.trading.macd2.models import SignalState as _SS
        _NEED = _ARM_NEED.get(_ARM, 0)
        _o_rm = worker._advance_held_position_risk_management
        _WCACHE = {"min": None, "bars": None, "mu": None}

        def _held_dir(sym):
            return _Dir.UP_RED if sym == config.LONG_SYMBOL else _Dir.DOWN_BLUE

        def _bars_now(now):
            """판정시점까지 **완성된** 3분봉만 (production resample 그대로). 분 단위 캐시."""
            m = now.replace(second=0, microsecond=0)
            if _WCACHE["min"] != m:
                _WCACHE["min"] = m
                _WCACHE["mu"] = None
                try:
                    _WCACHE["bars"] = _mff._prepare_bars(
                        _sew.resample_completed_3m(svc.get_history_df(), now=now))
                except Exception:
                    _WCACHE["bars"] = None
            return _WCACHE["bars"]

        def _mu_pattern(now, dirn):
            """롤링 MU 패턴. 앵커를 now-5분에 두면 production evaluate_pattern 의 T+2 창이
            정확히 '지금'이 된다 — 끝난 1분봉만 보므로 미래 데이터가 들어가지 않는다."""
            if _WCACHE["mu"] is None:
                try:
                    ser = _muw.load_mu_1m(now, config.MU_ASSIST_DATA_DIR or None)
                    anchor = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
                    _WCACHE["mu"] = str(_muw.evaluate_pattern(ser, dirn, anchor).get("mu_pattern_state"))
                except Exception:
                    _WCACHE["mu"] = _muw.PATTERN_UNAVAILABLE
            return _WCACHE["mu"]

        def _weak_signals(now, sym, cur_net):
            """추세약화 신호 6종. 전부 판정시점까지 확보된 데이터만 쓴다. 판정 불가는 False."""
            dirn = _held_dir(sym)
            sgn = 1.0 if dirn == _Dir.UP_RED else -1.0
            work = _bars_now(now)
            out = {}
            # ① HY 3분 MACD gap 이 직전 봉 대비 축소 (보유방향 부호)
            g_now = g_prev = None
            try:
                ser = _twfw._gap_series(work) if work is not None else None
                if ser is not None and len(ser) >= 2:
                    g_now = float(ser["gap"].iloc[-1]) * sgn
                    g_prev = float(ser["gap"].iloc[-2]) * sgn
            except Exception:
                pass
            out["s1_gap_shrink"] = bool(g_now is not None and g_prev is not None and g_now < g_prev)
            # ② MU 가 CONFIRM 을 봤다가 STALL/DIVERGE 로 약화
            pat = _mu_pattern(now, dirn)
            if pat == _muw.PATTERN_CONFIRM:
                ARM["mu_confirm"] = True
            out["s2_mu_weak"] = bool(ARM["mu_confirm"]
                                     and pat in (_muw.PATTERN_STALL, _muw.PATTERN_DIVERGE))
            # ③ 체결강도가 보유방향에서 약화 (production 정의 그대로, 기준 100 고정)
            try:
                cs = _xs.evaluate(dirn, now)
                out["s3_cttr_weak"] = bool(cs["state"] == _xs.MISMATCH)
            except Exception:
                out["s3_cttr_weak"] = False
            # ④ 최근 틱 고점 대비 0.5%p 이상 반납
            out["s4_giveback"] = bool(cur_net is not None and (ARM["peak"] - cur_net) >= 0.5)
            # ⑤ 1분봉 **또는** 3분봉이 VWAP 방향 우위를 잃음 (production _session_vwap 재사용)
            lost = False
            try:
                if work is not None and len(work):
                    vw = _mff._session_vwap(work)
                    v = float(vw.iloc[-1]) if len(vw) and pd.notna(vw.iloc[-1]) else None
                    if v and v > 0:
                        c3 = float(work["close"].iloc[-1])
                        lost = (c3 < v) if dirn == _Dir.UP_RED else (c3 > v)
                        if not lost:
                            d1 = svc.get_history_df()
                            if d1 is not None and len(d1):
                                c1 = float(d1["close"].iloc[-1])
                                lost = (c1 < v) if dirn == _Dir.UP_RED else (c1 > v)
            except Exception:
                pass
            out["s5_vwap_lost"] = bool(lost)
            # ⑥ 반대방향 플래그가 떴지만 아직 T+3 확정 전
            opp = "DOWN_BLUE" if dirn == _Dir.UP_RED else "UP_RED"
            out["s6_opp_pending"] = any(v == opp for v in OPP_PENDING.values())
            return out

        def _arm_rm(*, broker, state, market_data, now, quotes, pos, result):
            fired = _o_rm(broker=broker, state=state, market_data=market_data, now=now,
                          quotes=quotes, pos=pos, result=result)
            ep = int(getattr(state, "position_epoch", 0) or 0)
            if ARM["epoch"] != ep:
                ARM.update(epoch=ep, peak=0.0, line=None, prev_t=None,
                           cand=False, active=False, mu_confirm=False, sigs=())
            if fired or state.position is None or int(state.position.quantity or 0) <= 0:
                return fired
            sym, avg, qty = pos.symbol, float(pos.avg_price), int(pos.quantity)
            t0 = ARM["prev_t"] or (now - timedelta(seconds=_TICK_SEC))
            ARM["prev_t"] = now
            # 틱 최고수익: (직전 tick, 지금] 의 실제 1초 스냅샷 전부. 없으면 지금 시세 하나.
            w = _tick_window(sym, t0, now)
            if w is not None:
                hi = float(w["price"].max())
                last = float(w["price"].iloc[-1])
                lo_bid = float(w["best_bid"].min()) if w["best_bid"].notna().any() else None
            else:
                cur = quotes.get(sym) or px(sym, now)
                hi = last = (float(cur) if cur else None)
                lo_bid = BOOKQ.get(sym, (None, None))[0]
            if hi is not None:
                ARM["peak"] = max(ARM["peak"], worker._net_return_pct(sym, avg, hi, qty))
            cur_net = worker._net_return_pct(sym, avg, last, qty) if last is not None else None
            # ── 활성화 판정 ─────────────────────────────────────────────────
            if not ARM["active"]:
                if _arm_line(ARM["peak"]) is None:
                    return fired                       # 아직 +3% 미도달
                if not ARM["cand"]:
                    ARM["cand"] = True
                    ARM3LOG.append(dict(k="CAND", t=now.isoformat(), sym=sym,
                                        peak=round(ARM["peak"], 4), epoch=ep))
                if _NEED <= 0:
                    ARM["active"], ARM["sigs"] = True, ()
                else:
                    sg = _weak_signals(now, sym, cur_net)
                    hits = tuple(k for k, v in sg.items() if v)
                    if len(hits) < _NEED:
                        return fired                   # 추세약화 미확인 — 보호를 켜지 않는다
                    ARM["active"], ARM["sigs"] = True, hits
                    ARM3LOG.append(dict(k="ACTIVE", t=now.isoformat(), sym=sym,
                                        peak=round(ARM["peak"], 4), need=_NEED,
                                        sigs=list(hits), all=sg,
                                        cur_net=(round(cur_net, 4) if cur_net is not None else None),
                                        epoch=ep))
            # ── 보호선 갱신 (한 번 올라가면 내려가지 않는다) ─────────────────
            line = _arm_line(ARM["peak"])
            if line is not None and (ARM["line"] is None or line > ARM["line"]):
                ARM["line"] = line
                ARM3LOG.append(dict(k="ARM", t=now.isoformat(), sym=sym, peak=round(ARM["peak"], 4),
                                    line=ARM["line"], sigs=list(ARM["sigs"]), epoch=ep))
            if ARM["line"] is None or lo_bid is None:
                return fired
            bid_net = worker._net_return_pct(sym, avg, float(lo_bid), qty)
            if bid_net > ARM["line"]:
                return fired
            ARM3LOG.append(dict(k="BREACH", t=now.isoformat(), sym=sym, peak=round(ARM["peak"], 4),
                                line=ARM["line"], bid=float(lo_bid), bid_net=round(bid_net, 4),
                                sigs=list(ARM["sigs"]), epoch=ep))
            out = _oex.execute_exit(broker=broker, symbol=sym, quantity=qty,
                                    exit_reason=ARM_REASON, entry_price=avg,
                                    reconcile_retries=worker.ORDER_FILL_RECONCILE_RETRIES,
                                    reconcile_delay_sec=worker.ORDER_FILL_RECONCILE_DELAY_SEC)
            worker._apply_exit_outcome(state, out)
            if out.final_state == _SS.EXECUTED:
                state.time_window_position_active = False
                ARM3LOG.append(dict(k="EXIT", t=now.isoformat(), sym=sym, qty=qty,
                                    px=float(getattr(out.sell_result, "executed_price", 0.0) or 0.0),
                                    epoch=ep))
            result.actions.append(f"{ARM_REASON}:{sym}")
            return True
        monkeypatch.setattr(worker, "_advance_held_position_risk_management", _arm_rm)

    # ── 2026-11-01 ⑦ REGIME 별 자본배분 ───────────────────────────────────
    # WARMUP 은 TREND 와 같이 처리한다(둘 다 BASE 래더로 관리되는 쪽).
    REGALLOCLOG = []
    if (_BUD_TREND > 0 or _BUD_CHOP > 0 or _BUD_SLOT1 > 0 or _BUD_SLOT2 > 0
            or _BUD_EARLY > 0 or _BUD_LATE > 0):
        from app.trading.macd2 import chop_regime as _crA
        from app.trading.macd2 import position_sizing as _psA

        _bt = _BUD_TREND or 10_000_000.0
        _bc = _BUD_CHOP or 10_000_000.0

        def _chop_budget(st_, now_):
            """CHOP 진입에 쓸 예산. S축/E축이 켜져 있으면 그쪽이 우선한다."""
            if _BUD_SLOT1 > 0 or _BUD_SLOT2 > 0:
                # 이번 진입이 그날 몇 번째 슬롯인가 — resolve_slot 과 같은 출처
                used = int(getattr(st_, "tw2_3slot_slots_used_today", 0) or 0)
                return (_BUD_SLOT1 if used == 0 else _BUD_SLOT2) or _bc
            if _BUD_EARLY > 0 or _BUD_LATE > 0:
                k = now_.astimezone(config.KST)
                mins = k.hour * 60 + k.minute
                return (_BUD_EARLY if mins < _EARLY_CUT_MIN else _BUD_LATE) or _bc
            return _bc

        def _apply_budget(st_, where):
            if st_ is None:
                return
            reg = getattr(st_, "p3_last_regime", None)
            b = _chop_budget(st_, clock["now"]) if reg == _crA.REGIME_CHOP else _bt
            if float(getattr(st_, "budget", 0) or 0) != b:
                st_.budget = b
                REGALLOCLOG.append(dict(t=clock["now"].isoformat(), where=where,
                                        regime=str(reg), budget=b))

        _o_ps_eval = _psA.evaluate

        def _ps_eval(state_, **kw):
            # 사이징 직전 — 이 tick 의 regime 이 이미 갱신돼 있다
            _apply_budget(state_, "sizing")
            return _o_ps_eval(state_, **kw)
        monkeypatch.setattr(_psA, "evaluate", _ps_eval)

        _o_run_once = worker.run_once

        def _run_once(**kw):
            _apply_budget(kw.get("state"), "tick")
            return _o_run_once(**kw)
        monkeypatch.setattr(worker, "run_once", _run_once)

    # ── 2026-10-31 ⑥ 분산 (슬롯 상한 × 건당 예산) ─────────────────────────
    # 진입 게이트/청산 규칙은 한 줄도 바꾸지 않는다. 바꾸는 것은 둘뿐이다:
    #   · 하루 슬롯 상한 (chop_defense.daily_cap)
    #   · 건당 예산 (state.budget) — 사이징 공식 자체는 production 그대로
    if _SLOTCAP > 0:
        from app.trading.macd2 import chop_defense as _cdx
        monkeypatch.setattr(config, "CHOP_DAILY_CAP", _SLOTCAP)
        monkeypatch.setattr(config, "TW2_3SLOT_DAILY_CAP", _SLOTCAP)
    if _BUDGET > 0:
        state.budget = _BUDGET

    # ── 2026-10-31 ④ ARM 3% ONLY ─────────────────────────────────────────
    # 바꾸는 것은 **arm 시작점 하나**다:
    #   N1_TREND_TP1            0.035 -> _TARM/100   (매도비중 0.0 = arm 전용)
    #   MORNING_TRAILING_TRIGGER 0.035 -> _TARM/100
    # 보호선(N1_MORNING_TRAILING_STOP 1.5%)·TP2·손절·C1·B3·H50 은 그대로다.
    # tpm 은 import 시점에 퍼센트 상수를 굳히므로 그 모듈 상수도 같이 바꾼다.
    if _TARM > 0:
        monkeypatch.setattr(config, "N1_TREND_TP1", _TARM / 100.0)
        monkeypatch.setattr(config, "MORNING_TRAILING_TRIGGER", _TARM / 100.0)
        monkeypatch.setattr(tpm, "MORNING_TRAILING_TRIGGER_PCT", _TARM)

    # ── 2026-10-31 ③ 오후 XS VWAP 근접 override ───────────────────────────
    # MU ENTRY_CONFIRM 인 **오후** 후보가 **VWAP 사유 하나로만** 거절될 때,
    # 그 불리폭이 _VWOV% 이내면 그 거절만 푼다. 체결강도는 조건에서 제외한다.
    # 다른 TEG 사유(EMA stack / gap / spread / 시간성)가 하나라도 있으면 그대로 거절.
    # 슬롯·하루한도·시간창·MU ENTRY_BAD·CHOP MC·TP/SL 은 손대지 않는다.
    VWOVLOG = []

    if _VWOV > 0:
        from app.trading.macd2 import major_flag_filter as _mffv
        from app.trading.macd2 import mu_assist as _muv
        from app.trading.macd2 import signal_engine as _sev
        from app.trading.macd2 import teg_gate as _tegv
        from app.trading.macd2 import time_window_filter as _twfv
        from app.trading.macd2.models import Direction as _Dv

        _VWAP_ONLY = {config.TW2_REJECT_VWAP_VETO, _tegv.COND_VWAP}

        def _vw_dev(bars_3m, direction):
            """후보 방향 기준 VWAP 편차(%). 음수면 불리한 쪽. production _session_vwap 재사용."""
            try:
                work = _mffv._prepare_bars(bars_3m)
                if work is None or not len(work):
                    return None
                vw = _mffv._session_vwap(work)
                if not len(vw) or pd.isna(vw.iloc[-1]) or float(vw.iloc[-1]) <= 0:
                    return None
                sgn = 1.0 if direction == _Dv.UP_RED else -1.0
                return (float(work["close"].iloc[-1]) / float(vw.iloc[-1]) - 1.0) * 100.0 * sgn
            except Exception:
                return None

        def _vw_mu_confirm(now, direction):
            """롤링 MU ENTRY_CONFIRM (= MU CONFIRM ∧ HY gap EXPAND) — 끝난 1분봉만 본다."""
            try:
                ser = _muv.load_mu_1m(now, config.MU_ASSIST_DATA_DIR or None)
                anchor = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
                return str(_muv.evaluate_pattern(ser, direction, anchor).get(
                    "mu_pattern_state")) == _muv.PATTERN_CONFIRM
            except Exception:
                return False

        def _vw_ok(bars_3m, direction, now, reasons, where):
            """이 거절을 풀어도 되는가. 체결강도는 보지 않는다(기록만)."""
            rs = set(reasons or ())
            if not rs or not (rs <= _VWAP_ONLY):
                return False
            if now.astimezone(config.KST).time() < config.TW2_3SLOT_MORNING_WINDOW_END:
                return False                      # 오전은 대상 아님
            if not _vw_mu_confirm(now, direction):
                return False
            dev = _vw_dev(bars_3m, direction)
            ok = bool(dev is not None and -_VWOV <= dev < 0)
            try:
                cs = _xs.evaluate(direction, now)
            except Exception:
                cs = {}
            VWOVLOG.append(dict(t=now.isoformat(), where=where, dir=getattr(direction, "value", None),
                                reasons=sorted(rs), dev=(round(dev, 4) if dev is not None else None),
                                lim=_VWOV, mu_confirm=True, cttr=cs.get("cttr"),
                                cttr_state=cs.get("state"), applied=ok))
            return ok

        _o_veto_v = _twfv.evaluate_tw2_extra_vetoes

        def _veto_v(bars_3m, flag_direction, flag_bar_dt, decision_at):
            vetoed, reason = _o_veto_v(bars_3m, flag_direction, flag_bar_dt, decision_at)
            if vetoed and _vw_ok(bars_3m, flag_direction, decision_at, {reason}, "TW2_VETO"):
                return False, None
            return vetoed, reason
        monkeypatch.setattr(_twfv, "evaluate_tw2_extra_vetoes", _veto_v)

        _o_teg_v = _tegv.evaluate_teg

        def _teg_v(bars_3m, direction, flag_bar_dt, now, *a, **k):
            dec = _o_teg_v(bars_3m, direction, flag_bar_dt, now, *a, **k)
            if (not dec.approved) and _vw_ok(bars_3m, direction, now, dec.reject_reasons, "TEG"):
                return dataclasses.replace(dec, approved=True, reject_reasons=())
            return dec
        monkeypatch.setattr(_tegv, "evaluate_teg", _teg_v)

    # ── 2026-10-31 ② 청산 유예 ONE_BAR_HOLD / TWO_BAR_HOLD ────────────────
    # "1~2봉짜리 일시 휩쏘"만 골라 청산을 1~2개 완성 3분봉 늦춘다.
    # 무조건 늦추지 않는다 — 아래 **강한 붕괴** 중 하나라도 있으면 즉시 청산한다:
    #   ① MU 반대방향 ENTRY_CONFIRM(롤링)   ② HY 3분 MACD gap 이 반대방향으로 확대
    #   ③ 구조/박스 확정이탈(B3_SL)·hard stop  ④ 반대 플래그가 T+3 확정 + 진입게이트 통과
    # production 청산 규칙은 한 줄도 바꾸지 않는다. 유예 중에도 다른 청산은 그대로 돌고,
    # 유예가 끝나거나 강한 붕괴가 뜨면 **원래 사유 그대로** 집행한다.
    HOLDLOG = []
    DEFER = {"epoch": None, "reason": None, "t0": None, "until": None,
             "net0": None, "bars": 0, "sym": None}

    if _HOLD_BARS:
        from app.trading.macd2 import order_executor as _oexh
        from app.trading.macd2 import mu_assist as _muh
        from app.trading.macd2 import signal_engine as _seh
        from app.trading.macd2 import major_flag_filter as _mffh
        from app.trading.macd2 import time_window_filter as _twfh
        from app.trading.macd2.models import Direction as _Dh
        _o_exec_exit = _oexh.execute_exit
        _HCACHE = {"min": None, "bars": None, "mu": None}

        def _h_bars(now):
            m = now.replace(second=0, microsecond=0)
            if _HCACHE["min"] != m:
                _HCACHE["min"] = m
                _HCACHE["mu"] = None
                try:
                    _HCACHE["bars"] = _mffh._prepare_bars(
                        _seh.resample_completed_3m(svc.get_history_df(), now=now))
                except Exception:
                    _HCACHE["bars"] = None
            return _HCACHE["bars"]

        def _h_mu_opp(now, held):
            """반대방향 MU 롤링 패턴이 CONFIRM 인가 (강한 붕괴 ①)."""
            if _HCACHE["mu"] is None:
                opp = _Dh.DOWN_BLUE if held == _Dh.UP_RED else _Dh.UP_RED
                try:
                    ser = _muh.load_mu_1m(now, config.MU_ASSIST_DATA_DIR or None)
                    anchor = now.replace(second=0, microsecond=0) - timedelta(minutes=5)
                    _HCACHE["mu"] = str(_muh.evaluate_pattern(ser, opp, anchor).get("mu_pattern_state"))
                except Exception:
                    _HCACHE["mu"] = _muh.PATTERN_UNAVAILABLE
            return _HCACHE["mu"] == _muh.PATTERN_CONFIRM

        def _h_gap_opp_expand(now, held):
            """HY 3분 MACD gap 이 **반대방향으로 확대**되는가 (강한 붕괴 ②)."""
            sgn = 1.0 if held == _Dh.UP_RED else -1.0
            try:
                ser = _twfh._gap_series(_h_bars(now))
                if ser is None or len(ser) < 2:
                    return False
                g0 = float(ser["gap"].iloc[-1]) * sgn
                g1 = float(ser["gap"].iloc[-2]) * sgn
                return bool(g0 < 0 and g0 < g1)       # 반대부호 ∧ 더 벌어짐
            except Exception:
                return False

        def _h_opp_entry_approved(held):
            """반대 플래그가 T+3 확정 + 진입게이트 통과했는가 (강한 붕괴 ④)."""
            opp = "DOWN_BLUE" if held == _Dh.UP_RED else "UP_RED"
            return any(v == opp for v in OPP_APPROVED.values())

        def _h_breakdown(now, held, reason):
            if any(k in reason for k in _HOLD_NEVER):
                return "HARD"
            if _h_opp_entry_approved(held):
                return "OPP_ENTRY_APPROVED"
            if _h_gap_opp_expand(now, held):
                return "GAP_OPP_EXPAND"
            if _h_mu_opp(now, held):
                return "MU_OPP_CONFIRM"
            return None

        def _exec_exit_defer(*, broker, symbol, quantity, exit_reason, entry_price, **kw):
            now = clock["now"]
            st = STATE.get("s")
            pos = getattr(st, "position", None) if st is not None else None
            ep = int(getattr(st, "position_epoch", 0) or 0) if st is not None else 0
            reason = str(exit_reason or "")
            held = _Dh.UP_RED if symbol == config.LONG_SYMBOL else _Dh.DOWN_BLUE
            avg = float(entry_price or (pos.avg_price if pos else 0.0))
            bid = (BOOKQ.get(symbol) or (None, None))[0] or px(symbol, now)
            net = worker._net_return_pct(symbol, avg, float(bid), int(quantity)) if bid else None

            if DEFER["epoch"] != ep:
                DEFER.update(epoch=ep, reason=None, t0=None, until=None, net0=None, bars=0, sym=None)

            target = any(k in reason for k in _HOLD_TARGET) and not any(k in reason for k in _HOLD_NEVER)
            if not target:
                return _o_exec_exit(broker=broker, symbol=symbol, quantity=quantity,
                                    exit_reason=exit_reason, entry_price=entry_price, **kw)

            bd = _h_breakdown(now, held, reason)
            if bd:
                HOLDLOG.append(dict(k="BREAKDOWN", t=now.isoformat(), sym=symbol, reason=reason,
                                    why=bd, net=(round(net, 4) if net is not None else None), epoch=ep))
                return _o_exec_exit(broker=broker, symbol=symbol, quantity=quantity,
                                    exit_reason=exit_reason, entry_price=entry_price, **kw)

            if DEFER["reason"] is None:
                b0, b1 = worker.forming_bar_window(now)
                DEFER.update(reason=reason, t0=now.isoformat(), bars=0, sym=symbol,
                             net0=net, until=b1 + timedelta(minutes=3 * (_HOLD_BARS - 1)))
                HOLDLOG.append(dict(k="DEFER", t=now.isoformat(), sym=symbol, reason=reason,
                                    until=DEFER["until"].isoformat(),
                                    net0=(round(net, 4) if net is not None else None), epoch=ep))
                return _oexh.ExecutionOutcome("", _Dh.HOLD, symbol, _SSh.DETECTED,
                                              block_reason="RESEARCH_HOLD_DEFER")
            if now < DEFER["until"]:
                return _oexh.ExecutionOutcome("", _Dh.HOLD, symbol, _SSh.DETECTED,
                                              block_reason="RESEARCH_HOLD_DEFER")
            # 유예창 종료 — 원래 방향을 회복했으면 HOLD(청산 취소), 아니면 원래 사유로 청산
            rec = bool(net is not None and DEFER["net0"] is not None and net > DEFER["net0"])
            HOLDLOG.append(dict(k=("RECOVER" if rec else "EXIT_AFTER_DEFER"), t=now.isoformat(),
                                sym=symbol, reason=reason,
                                net0=(round(DEFER["net0"], 4) if DEFER["net0"] is not None else None),
                                net=(round(net, 4) if net is not None else None), epoch=ep))
            DEFER.update(reason=None, t0=None, until=None, net0=None, sym=None)
            if rec:
                return _oexh.ExecutionOutcome("", _Dh.HOLD, symbol, _SSh.DETECTED,
                                              block_reason="RESEARCH_HOLD_RECOVERED")
            return _o_exec_exit(broker=broker, symbol=symbol, quantity=quantity,
                                exit_reason=exit_reason, entry_price=entry_price, **kw)

        from app.trading.macd2.models import SignalState as _SSh
        monkeypatch.setattr(_oexh, "execute_exit", _exec_exit_defer)
    # ── 2026-10-29 PARITY + P70R 연구 변형 (하네스 전용 monkeypatch) ──────
    PAR_ON = VAR in ("PAR", "PAR70", "PAR50")
    P70R_RATIO = {"PAR70": 0.70, "PAR50": 0.50}.get(VAR)
    RUNNER = os.environ.get("WK_RUNNER", "N1")
    RUNNER_TP = {"N1": None, "TP15": 1.5, "TP30": 3.0}[RUNNER]
    P70R_REB = float(os.environ.get("WK_P70R_REB", "0.6"))
    RUN = {"epoch": None, "armed_at": None, "low3": None, "reb": None}
    P70RLOG = []

    def _p70r_gate(sym):
        """손절을 띄운 3분봉의 저점과, 그 시점의 두 가지 반등% 를 돌려준다.

        low3      : 그 3분봉의 실제 1분봉 저점 (사용자 정의 "직전 3분 저점")
        reb_fill  : **체결가**(= broker 호가 = px(t+FILL_LAG)) 기준 반등% — 기존 연구 정의
        reb_dec   : 판정 틱의 실시간 호가 기준 반등% — 지연 없이 즉시 집행했다면
        """
        now_ = clock["now"]
        b0, _b1 = worker.forming_bar_window(now_)
        lo = None
        ix_ = idx.get(sym)
        if ix_ is not None:
            w_ = ix_[(ix_.index >= b0 - timedelta(minutes=3)) & (ix_.index < b0)]
            if len(w_):
                lo = float(w_["low"].min())
        fill_px = broker._quotes.get(sym)
        dec_px = px(sym, now_)
        return (lo,
                (float(fill_px) / lo - 1) * 100.0 if (lo and fill_px) else None,
                (float(dec_px) / lo - 1) * 100.0 if (lo and dec_px) else None)

    if PAR_ON:
        _o_par_pm = tpm.evaluate_position
        _o_par_tp = tpm.evaluate_take_profit_immediate

        def _par_epoch():
            st_ = STATE.get("s")
            return int(getattr(st_, "position_epoch", 0) or 0) if st_ is not None else 0

        def _par_pm(**kw):
            dec = _o_par_pm(**kw)
            st_ = STATE.get("s")
            pos_ = getattr(st_, "position", None) if st_ is not None else None
            if pos_ is None:
                return dec
            net_ = float(kw.get("net_return_pct") or 0.0)
            if RUN["epoch"] == _par_epoch():
                # runner 구간 — 손절선 1회 면제 + 사전등록 runner 익절
                if dec.exit_reason == config.EXIT_TW_STOP_LOSS:
                    P70RLOG.append(dict(k="RUNNER_SL_WAIVED", t=clock["now"].isoformat(), net=net_))
                    return dataclasses.replace(dec, exit_reason=None, sell_fraction=0.0,
                                               label="P70R_RUNNER_SL_WAIVED")
                if RUNNER_TP is not None and net_ >= RUNNER_TP:
                    P70RLOG.append(dict(k="RUNNER_TP", t=clock["now"].isoformat(), net=net_, lvl=RUNNER_TP))
                    return dataclasses.replace(dec, exit_reason="P70R_RUNNER_TP", sell_fraction=1.0,
                                               label="P70R_RUNNER_TP")
                return dec
            if dec.exit_reason != config.EXIT_TW_STOP_LOSS:
                return dec
            if str(kw.get("session") or "MORNING") != "MORNING" or bool(kw.get("tp1_done")):
                return dec      # 오전 TP1 이전 손절 가지만이 대상
            lo, reb_fill, reb_dec = _p70r_gate(pos_.symbol)
            opened = reb_fill is not None and reb_fill >= P70R_REB
            P70RLOG.append(dict(k="GATE", t=clock["now"].isoformat(), sym=pos_.symbol, net=net_,
                                low3=lo, reb_fill=reb_fill, reb_dec=reb_dec,
                                open=bool(opened), ratio=P70R_RATIO, qty=int(pos_.quantity)))
            if P70R_RATIO is None or not opened or pos_.quantity < 2:
                return dec
            RUN.update(epoch=_par_epoch(), armed_at=clock["now"].isoformat(), low3=lo, reb=reb_fill)
            return dataclasses.replace(dec, exit_reason="P70R_PARTIAL_STOP",
                                       sell_fraction=float(P70R_RATIO), tp1_done=False,
                                       label="P70R_PARTIAL_STOP")

        def _par_tp(**kw):
            if RUNNER_TP is None or RUN["epoch"] != _par_epoch():
                return _o_par_tp(**kw)
            # runner 는 사전등록 TP 만 본다 — 기존 틱 TP 래더는 건너뛴다
            net_ = float(kw.get("net_return_pct") or 0.0)
            if net_ >= RUNNER_TP:
                P70RLOG.append(dict(k="RUNNER_TP_TICK", t=clock["now"].isoformat(), net=net_, lvl=RUNNER_TP))
                return tpm.PositionManagementDecision("P70R_RUNNER_TP", 1.0, True,
                                                      max(0.0, net_), "P70R_RUNNER_TP")
            return tpm.PositionManagementDecision(None, 0.0, bool(kw.get("tp1_done")),
                                                  max(0.0, net_), "P70R_RUNNER_HOLD")

        monkeypatch.setattr(tpm, "evaluate_position", _par_pm)
        monkeypatch.setattr(tpm, "evaluate_take_profit_immediate", _par_tp)
    FDT = type("FDT", (datetime,), {"now": classmethod(lambda c, tz=None: clock["now"].astimezone(tz) if tz else clock["now"])})
    monkeypatch.setattr(worker, "datetime", FDT)
    orders, acts = [], []
    t = clock["now"]
    end = _d(D).replace(hour=15, minute=21)
    end = min(end, hy["datetime"].iloc[-1].to_pydatetime() + timedelta(minutes=1))
    while t <= end:
        if _d(P).replace(hour=15, minute=21) < t < _d(D).replace(hour=8):
            t = _d(D).replace(hour=8)
            continue
        clock["now"] = t
        with svc._history_lock:
            svc._df_1m = hy[hy["datetime"] + pd.Timedelta(minutes=1) + _HIST_LAG <= pd.Timestamp(t)].reset_index(drop=True)
        for sym in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):
            p = _set_book(sym, t + _FILL_LAG)
            if p is not None:
                broker.set_quote(sym, p)
        n0 = len(broker.orders)
        r = worker.run_once(broker=broker, market_data=svc, state=state, now=t)
        today = t.strftime("%Y%m%d") == D
        for o in broker.orders[n0:]:
            if o.success and today:
                orders.append(dict(t=t.isoformat(), side=o.side, sym=o.symbol, qty=int(o.executed_qty), px=float(o.executed_price),
                                   acts=list(r.actions)))
        if today:
            tr = getattr(r, "signal_dispatch_trace", None) or {}
            for k in ("e_early_pass", "e_rs", "e_breakout"):
                if k in tr:
                    LOG.append(dict(ev=k, t=t.isoformat(), armed_at=t.isoformat(), **{kk: (str(vv) if not isinstance(vv, (int, float, bool, type(None), str)) else vv) for kk, vv in tr[k].items()}))
            for a in r.actions:
                if a.startswith("E_PENDING") or a.startswith("E_BREAKOUT"):
                    LOG.append(dict(ev=a, t=t.isoformat(), armed_at=t.isoformat()))
        if today and r.actions:
            acts.append(dict(t=t.isoformat(), a=list(r.actions),
                             mode=p3_stack.position_mode(state) if state.position else "-", regime=state.p3_last_regime))
        _nxt = t + timedelta(seconds=_TICK_SEC)
        if _FINE_TICK:
            _pre = worker.forming_bar_window(t)[1] - timedelta(seconds=1)
            if t < _pre < _nxt:
                _nxt = _pre
        t = _nxt
    if PEND["rec"] is not None:
        LOG.append(_pub(PEND["rec"], ev="EXPIRED_EOD", at=end.isoformat()))
        PEND["rec"] = None
    sig = []
    for r in ledger.load_signal_ledger(limit=0):
        if str(r.get("trading_date")) == D:
            sig.append({k: (str(v) if v is not None else None) for k, v in r.items()
                        if k in ("signal_id", "completed_bar_at", "order_result", "block_reason", "final_qty",
                                 "order_price", "detected_at", "direction")})
    ex = []
    for r in ledger.load_execution_ledger(limit=0):
        ex.append({k: (str(v) if v is not None else None) for k, v in r.items()
                   if k in ("side", "symbol", "executed_qty", "executed_price", "exit_reason", "net_pnl", "gross_pnl",
                            "fee", "signal_id", "timestamp")})
    Path(os.environ["WK_OUT"]).write_text(json.dumps(dict(D=D, FILL=FILL, VAR=VAR, orders=orders, acts=acts, sig=sig, ex=ex, entries=[e for e in ENTRIES if e["t"][:10].replace("-", "") == D], lock_events=[e for e in LOCK["events"] if e["t"][:10].replace("-", "") == D], lock_blocked=[e for e in LOCK["blocked"] if e["t"][:10].replace("-", "") == D],
                                                     brk=[e for e in LOG if e["armed_at"][:10].replace("-", "") == D],
                                                     prod=[e for e in PRODLOG if (e.get("t") or "")[:10].replace("-", "") == D],
                                                     gates=GATELOG, arm3=ARM3LOG, hold=HOLDLOG, hold_kind=_HOLD, vwov=VWOVLOG, vwov_lim=_VWOV, trail_arm=_TARM, trend_bypass=bool(_TRBP), slot_cap=_SLOTCAP, budget_ovr=_BUDGET, regalloc=REGALLOCLOG, bud_trend=_BUD_TREND, bud_chop=_BUD_CHOP, bud_slot1=_BUD_SLOT1, bud_slot2=_BUD_SLOT2, bud_early=_BUD_EARLY, bud_late=_BUD_LATE, arm=dict(kind=_ARM, mode=_MODE, mua=bool(_MUA), xs=bool(_XS), cukill=bool(_CUKILL), tickpx=bool(_TICKPX)), p70r=P70RLOG, par=dict(hist_lag=int(_HIST_LAG.total_seconds()), fill_lag=int(_FILL_LAG.total_seconds()), fine_tick=bool(_FINE_TICK), runner=os.environ.get('WK_RUNNER', 'N1'), reb=float(os.environ.get('WK_P70R_REB', '0.6'))), mode_state=strategy_mode.current(state), mc_on=state.chop_mc_on_at, mc_off=state.chop_mc_off_at, q2=Q2LOG, oppq=OPPQLOG, mc_block_exit=os.environ.get('WK_MC_BLOCK_EXIT') == '1',
                                                     slow_cfg=bool(config.SLOW_TP2_ENABLED)),
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
