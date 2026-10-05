"""TEMP (실행 후 즉시 삭제) — 9월 전략 비교용 production worker 재생 하네스 v2. 모든 쓰기 tmp, production 코드 무수정.

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


def _d(s):
    return datetime.strptime(s, "%Y%m%d").replace(tzinfo=KST)


class RealFillBroker(FakeBroker):
    """호가 1틱 스프레드 모형: ask1 = 시세 + 5원, bid1 = 시세."""

    def _ask(self, symbol):
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
            if VAR == "E" and regime == _cr.REGIME_CHOP and snap.ok:
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

    # ── CHOP STAGED ENTRY (STG50 = 50/50, STG30 = 30/70) ────────────────
    # 평소에는 현행 P3 와 완전히 동일하다. 진입 직전 **최근 30분**이
    # "저효율(PE<0.20) + 저변동(realized range<1.0%)" 왕복장으로 판정될 때만
    # T+3 승인 시 일부만 진입하고, (플래그봉, T+3 확정봉) 극값을 돌파하면
    # 15분 안에 나머지를 추가한다. 미도달이면 추가진입만 취소한다.
    # 첫 부분진입 포지션은 production P3 경로 그대로 관리된다.
    STG = {"STG50": 0.5, "STG30": 0.3}.get(VAR)
    PE_MAX, RNG_MAX, WIN_MIN, ADD_WAIT_MIN = 0.20, 1.0, 30, 15
    PEND = {"rec": None}
    CTX = {"bars_3m": None, "flag_bar_ts": None}
    STG_LOG = []

    def _chop30(now):
        """최근 30분 (PE, RNG). 당일 완성봉만 사용 — calib.py 와 같은 정의."""
        d = etf.get(config.WATCH_SYMBOL)
        if d is None:
            return None
        ts = pd.Timestamp(now)
        done = d[(d["datetime"] + pd.Timedelta(minutes=1) <= ts)
                 & (d["datetime"].dt.date == ts.date())]
        if len(done) < WIN_MIN:
            return None
        w = done.iloc[-WIN_MIN:]
        c = w["close"].to_numpy(dtype=float)
        path = float(abs(pd.Series(c).diff()).sum())
        if path <= 0:
            return None
        return abs(c[-1] - c[0]) / path, float((w["high"].max() - w["low"].min()) / c[-1] * 100.0)

    def _is_chop(now):
        f = _chop30(now)
        return (f is not None and f[0] < PE_MAX and f[1] < RNG_MAX), f

    def _trigger_px(bars_3m, flag_bar_dt, confirm_bar_dt, direction):
        ts = pd.to_datetime(bars_3m["datetime"])
        rows = []
        for dt in (flag_bar_dt, confirm_bar_dt):
            if dt is None:
                continue
            m = bars_3m[ts == pd.Timestamp(dt)]
            if len(m):
                rows.append(m.iloc[-1])
        if not rows:
            return None
        if direction == Direction.UP_RED:
            return max(float(r["high"]) for r in rows)
        return min(float(r["low"]) for r in rows)

    def _hy_px(now):
        """기초자산 호가 — REAL 체결모형과 같은 분봉 시가→종가 선형보간."""
        ix = idx.get(config.WATCH_SYMBOL)
        ts = pd.Timestamp(now)
        m = ts.floor("min")
        if ix is not None and m in ix.index:
            r = ix.loc[m]
            if isinstance(r, pd.DataFrame):
                r = r.iloc[-1]
            return float(r["open"]) + (float(r["close"]) - float(r["open"])) * ((ts - m).total_seconds() / 60.0)
        d = etf.get(config.WATCH_SYMBOL)
        if d is None:
            return None
        done = d[d["datetime"] + pd.Timedelta(minutes=1) <= ts]
        return float(done["close"].iloc[-1]) if len(done) else None

    def _pub(rec, **extra):
        return dict(sid=rec["signal_id"], dir=rec["dir"], trigger=rec["trigger"],
                    pe=rec["pe"], rng=rec["rng"], first_qty=rec["first_qty"],
                    armed_at=pd.Timestamp(rec["armed_at"]).isoformat(),
                    deadline=pd.Timestamp(rec["deadline"]).isoformat(), **extra)

    def _held_qty(broker, sym):
        p = broker.get_position(sym)
        return int(p.quantity) if p is not None else 0

    if STG:
        _orig_resolve = worker._resolve_tw2_3slot_candidate
        _orig_eow = worker._execute_or_wait

        def _eow(*, broker, market_data, state, now, macd_snap, direction, signal_id, signal_type,
                 position, result, signal_detected_at=None, budget_multiplier=1.0):
            if not str(signal_id).endswith(":TW2_3SLOT_CONFIRM"):
                return _orig_eow(broker=broker, market_data=market_data, state=state, now=now,
                                 macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                                 signal_type=signal_type, position=position, result=result,
                                 signal_detected_at=signal_detected_at, budget_multiplier=budget_multiplier)
            chop, f = _is_chop(now)
            if not chop:
                return _orig_eow(broker=broker, market_data=market_data, state=state, now=now,
                                 macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                                 signal_type=signal_type, position=position, result=result,
                                 signal_detected_at=signal_detected_at, budget_multiplier=budget_multiplier)
            full = float(budget_multiplier or 1.0)
            out = _orig_eow(broker=broker, market_data=market_data, state=state, now=now,
                            macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                            signal_type=signal_type, position=position, result=result,
                            signal_detected_at=signal_detected_at, budget_multiplier=full * STG)
            if out is None or out.final_state != worker.SignalState.EXECUTED:
                STG_LOG.append(dict(sid=signal_id, dir=direction.value, trigger=None, pe=f[0], rng=f[1],
                                    first_qty=0, armed_at=now.isoformat(), deadline=now.isoformat(),
                                    ev="FIRST_NOT_EXECUTED"))
                return out
            trig = _trigger_px(CTX["bars_3m"], worker._parse_iso_dt(CTX["flag_bar_ts"]),
                               macd_snap.bar_dt, direction)
            rec = dict(direction=direction, dir=direction.value, signal_id=signal_id, trigger=trig,
                       pe=f[0], rng=f[1], first_qty=int(out.quantity or 0),
                       symbol=out.target_symbol, rest_mult=full * (1.0 - STG),
                       epoch=int(state.position_epoch or 0) + 1,
                       armed_at=now, deadline=now + timedelta(minutes=ADD_WAIT_MIN))
            if trig is None:
                STG_LOG.append(_pub(rec, ev="NO_TRIGGER"))
                return out
            PEND["rec"] = rec
            STG_LOG.append(_pub(rec, ev="STAGED_FIRST", first_px=float(out.filled_avg_price or 0.0)))
            return out

        def _add_on(*, broker, market_data, state, now, rec, price):
            """돌파 시 나머지 비중 추가 — production order_executor 그대로 사용.
            position=None 으로 넘겨야 BLOCK_ALREADY_HOLDING 을 거치지 않고 매수 레그만 탄다."""
            sym = rec["symbol"]
            before = _held_qty(broker, sym)
            _, quotes = worker._quote_status_for_order(market_data, (sym,))
            if not quotes.get(sym):
                STG_LOG.append(_pub(rec, ev="ADD_NO_QUOTE", at=now.isoformat()))
                return
            out = worker.order_executor.execute_signal(
                broker=broker, direction=rec["direction"], signal_id=rec["signal_id"] + ":ADD",
                quotes=quotes, position=None,
                budget=float(state.budget or 0.0) * float(rec["rest_mult"]),
                processed_signal_ids=frozenset(),
                reconcile_retries=worker.ORDER_FILL_RECONCILE_RETRIES,
                reconcile_delay_sec=worker.ORDER_FILL_RECONCILE_DELAY_SEC,
            )
            added = _held_qty(broker, sym) - before
            if added > 0:
                # 부분청산 비중이 전체 수량 기준이 되도록 초기수량을 갱신하고,
                # 일일 노출 누계에 나머지 비중을 반영한다(진입 순번은 올리지 않는다).
                state.time_window_initial_quantity = int(state.time_window_initial_quantity or 0) + added
                state.x2lite_exposure_used_today = round(
                    float(getattr(state, "x2lite_exposure_used_today", 0.0) or 0.0) + float(rec["rest_mult"]), 6)
            STG_LOG.append(_pub(rec, ev="ADDED" if added > 0 else "ADD_FAILED", at=now.isoformat(),
                                price=price, added_qty=added,
                                add_px=float(getattr(out, "filled_avg_price", 0.0) or 0.0),
                                block=(out.block_reason or None),
                                delay_min=(now - pd.Timestamp(rec["armed_at"]).to_pydatetime()).total_seconds() / 60.0))

        def _resolve_stg(*, broker, market_data, state, now, macd_snap, bars_3m, df_1m, position, result):
            rec = PEND["rec"]
            if rec is not None:
                drop = None
                if now.date() != pd.Timestamp(rec["armed_at"]).date() or now > rec["deadline"]:
                    drop = "EXPIRED"
                elif state.position is None or _held_qty(broker, rec["symbol"]) <= 0:
                    drop = "CANCELLED_FLAT"        # 추가 전에 이미 청산됨
                elif int(state.position_epoch or 0) != rec["epoch"]:
                    drop = "CANCELLED_EPOCH"       # 다른 포지션으로 교체됨
                if drop:
                    PEND["rec"] = None
                    STG_LOG.append(_pub(rec, ev=drop, at=now.isoformat()))
                else:
                    p = _hy_px(now)
                    hit = p is not None and ((p >= rec["trigger"]) if rec["dir"] == "UP_RED" else (p <= rec["trigger"]))
                    if hit:
                        PEND["rec"] = None
                        _add_on(broker=broker, market_data=market_data, state=state, now=now, rec=rec, price=p)
            CTX["bars_3m"] = bars_3m
            CTX["flag_bar_ts"] = state.tw2_3slot_pending_flag_bar_ts
            return _orig_resolve(broker=broker, market_data=market_data, state=state, now=now,
                                 macd_snap=macd_snap, bars_3m=bars_3m, df_1m=df_1m,
                                 position=position, result=result)
        monkeypatch.setattr(worker, "_execute_or_wait", _eow)
        monkeypatch.setattr(worker, "_resolve_tw2_3slot_candidate", _resolve_stg)

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
    Broker = RealFillBroker if FILL == "REAL" else FakeBroker
    broker = Broker(cash=50_000_000.0, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 6_000.0})
    state = state_store.default_state()
    state.auto_trade_on = True
    state.mode = "mock"
    state.budget = 10_000_000.0
    strategy_mode.apply(state, strategy_mode.MODE_P3 if VAR in ("A", "E", "F", "L30", "L45", "ALT3", "STG50", "STG30") else strategy_mode.MODE_N1)
    STATE["s"] = state
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
            svc._df_1m = hy[hy["datetime"] + pd.Timedelta(minutes=1) <= pd.Timestamp(t)].reset_index(drop=True)
        for sym in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):
            p = px(sym, t)
            if p is not None:
                broker.set_quote(sym, p)
        n0 = len(broker.orders)
        r = worker.run_once(broker=broker, market_data=svc, state=state, now=t)
        today = t.strftime("%Y%m%d") == D
        for o in broker.orders[n0:]:
            if o.success and today:
                orders.append(dict(t=t.isoformat(), side=o.side, sym=o.symbol, qty=int(o.executed_qty), px=float(o.executed_price),
                                   acts=list(r.actions)))
        if today and r.actions:
            acts.append(dict(t=t.isoformat(), a=list(r.actions),
                             mode=p3_stack.position_mode(state) if state.position else "-", regime=state.p3_last_regime))
        t += timedelta(seconds=20)
    if PEND["rec"] is not None:                      # 장 종료 시점에 남은 추가진입 trigger
        STG_LOG.append(_pub(PEND["rec"], ev="EXPIRED_EOD", at=end.isoformat()))
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
                                                     stg=[e for e in STG_LOG if e["armed_at"][:10].replace("-", "") == D]),
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
