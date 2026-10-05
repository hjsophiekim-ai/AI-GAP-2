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
    # ── F = RSBRK : RS 상위20% x1.25 증액  +  (오전 ∧ C1 chop) 돌파확인 지연 ──
    #   지연 대상: 12시 이전 ∧ PE<0.20 ∧ 최근30분 range<1.0% ∧ EMA20 교차>=3
    #   지연 규칙: T+3 승인 시 매수하지 않고 (플래그봉, 확정봉) 극값 ±1틱을 trigger 로 걸어
    #             30분 안에 돌파하면 그 시점에 production 경로로 진입, 미도달이면 폐기.
    #   그 외 플래그는 현행 그대로 진입하되 RS 상위20% 면 비중만 x1.25.
    RSBRK = (VAR in ("EARLYBRK15", "REFTP"))
    # REFERENCE-TP: 돌파대기 후 늦게 체결된 거래만 익절 기준가격을
    # 실제 체결가가 아니라 T+3 승인시점의 원래 P3 reference entry price
    # 기준으로 계산한다. 손절/트레일링/max-hold 는 실제 체결가 기준 유지.
    REFTP = (VAR == "REFTP")
    REF = {"px": None, "ret": None}
    RS_UP, WAIT_MIN, BUF_TICKS, HY_TICK = 1.25, 15, 0.0, 500.0
    PE_MAX, RNG_MAX, XC_MIN, AM_HOUR = 0.20, 1.0, 3, 12
    RS_TBL = json.loads(Path(os.environ["RS_TABLE"]).read_text(encoding="utf-8")) if RSBRK else {}
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
        c1 = dist <= 0.2
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
        return bool(c1 and c2 and c3), dict(dist=round(float(dist), 4), c1=bool(c1), c2=bool(c2), c3=bool(c3))

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
                    return _orig_eow(broker=broker, market_data=market_data, state=state, now=now,
                                     macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                                     signal_type=signal_type, position=position, result=result,
                                     signal_detected_at=signal_detected_at, budget_multiplier=bm)
                EP["wait"] += 1
                if _d:
                    LOG.append(dict(ev="EARLY_FAIL", t=now.isoformat(), sid=sid, dir=direction.value,
                                    armed_at=now.isoformat(), **_d))
            if is3 and FIRE["on"] is None and _am_chop(now):
                flag_bar_dt = worker._parse_iso_dt(CTX["flag_bar_ts"])
                trig = _trigger_px(CTX["bars_3m"], flag_bar_dt, macd_snap.bar_dt, direction)
                if trig is not None:
                    _rtgt = worker.order_executor.target_symbol_for_direction(direction)
                    _rpx = px(_rtgt, now)
                    rec = dict(direction=direction, dir=direction.value, signal_id=sid,
                               flag_bar_dt=flag_bar_dt, trigger=trig, armed_at=now,
                               deadline=now + timedelta(minutes=WAIT_MIN),
                               # 승인시점에 P3 가 실제로 샀을 가격(= ask1 = 시세 + 1틱).
                               ref_px=(None if _rpx is None else float(_rpx) + ETF_TICK),
                               ref_sym=_rtgt,
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
            # C 는 사이징을 바꾸지 않는다 (production 일일 상한 그대로 작동)
            return _orig_eow(broker=broker, market_data=market_data, state=state, now=now,
                             macd_snap=macd_snap, direction=direction, signal_id=signal_id,
                             signal_type=signal_type, position=position, result=result,
                             signal_detected_at=signal_detected_at, budget_multiplier=bm)

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
                    if ok and REFTP and rec.get("ref_px"):
                        REF["px"] = float(rec["ref_px"])
                    LOG.append(_pub(rec, ev="FIRED", at=now.isoformat(), price=p, executed=ok,
                                    ref_px=rec.get("ref_px"),
                                    fill_px=(float(state.position.avg_price) if state.position is not None else None),
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

    # ── REFERENCE-TP (2026-10-05) ──────────────────────────────────────
    # 익절(TP) 판정만 '승인시점 참조가격 기준 수익률' 로 바꾸고, 손절/트레일링/
    # after-TP1-stop/max-hold/C1/ETP 는 전부 **실제 체결가 기준 그대로** 둔다.
    # 적용 대상은 REF["px"] 가 세팅된 포지션 = 돌파대기 후 늦게 체결된 거래뿐이다.
    # 즉시진입 거래는 REF["px"] 가 None 이라 한 줄도 바뀌지 않는다.
    if REFTP:
        from app.trading.macd2 import time_window_position_manager as _tpm
        from app.trading.macd2 import p3_stack as _p3s

        TP_REASONS = {config.EXIT_TW_TP1_PARTIAL, config.EXIT_TW_TP2_FULL,
                      config.EXIT_TW_AFTERNOON_TP}
        B3_TP_REASONS = {config.EXIT_B3_TP, config.EXIT_P3_PARTIAL,
                         config.EXIT_P3_H30_PARTIAL}

        _o_nr = worker._net_return_pct

        def _nr(symbol, entry_price, current_price, quantity):
            """실제 수익률을 그대로 돌려주되, 같은 현재가에 대한 **참조 수익률**을
            옆에 적어 둔다. 아래 TP 판정만 그 값을 쓴다."""
            r = _o_nr(symbol, entry_price, current_price, quantity)
            rp = REF["px"]
            REF["ret"] = (_o_nr(symbol, float(rp), current_price, quantity)
                          if rp else None)
            return r

        monkeypatch.setattr(worker, "_net_return_pct", _nr)

        _o_tpi = _tpm.evaluate_take_profit_immediate

        def _tpi(**kw):
            # 이 함수는 **익절만** 돌려준다(손절 계열을 절대 반환하지 않는다).
            if REF["ret"] is None:
                return _o_tpi(**kw)
            return _o_tpi(**dict(kw, net_return_pct=REF["ret"]))

        monkeypatch.setattr(_tpm, "evaluate_take_profit_immediate", _tpi)

        _o_ep = _tpm.evaluate_position

        def _ep(**kw):
            act = _o_ep(**kw)
            if REF["ret"] is None:
                return act
            ref = _o_ep(**dict(kw, net_return_pct=REF["ret"]))
            if ref.exit_reason in TP_REASONS:
                # 익절은 참조가 기준. peak 추적기는 실제가 기준 값을 유지한다
                # (트레일링/after-TP1-stop 이 그 값을 쓴다).
                return dataclasses.replace(ref, peak_net_return=act.peak_net_return)
            if act.exit_reason in TP_REASONS:
                # 실제가로는 익절이지만 참조가로는 아니다 -> 익절하지 않는다.
                return dataclasses.replace(
                    act, exit_reason=None, sell_fraction=0.0,
                    label=f"{act.label}+REFTP_HOLD")
            return act

        monkeypatch.setattr(_tpm, "evaluate_position", _ep)

        _o_p3 = _p3s.evaluate

        def _p3e(**kw):
            act = _o_p3(**kw)
            if REF["ret"] is None:
                return act
            ref = _o_p3(**dict(kw, net_return_pct=REF["ret"]))
            if ref.exit_reason in B3_TP_REASONS:
                return ref
            if act.exit_reason in B3_TP_REASONS:
                return _p3s.B3Decision(action=_p3s.ACTION_HOLD,
                                       reason="reftp_hold", net_pct=float(act.net_pct))
            return act

        monkeypatch.setattr(_p3s, "evaluate", _p3e)

        monkeypatch.setattr(worker, "_resolve_tw2_3slot_candidate", _resolve_rb)

    Broker = RealFillBroker if FILL == "REAL" else FakeBroker
    broker = Broker(cash=50_000_000.0, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 6_000.0})
    state = state_store.default_state()
    state.auto_trade_on = True
    state.mode = "mock"
    state.budget = 10_000_000.0
    strategy_mode.apply(state, strategy_mode.MODE_P3 if VAR in ("A", "E", "F", "L30", "L45", "ALT3", "EARLYBRK15", "REFTP") else strategy_mode.MODE_N1)
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
        # 포지션이 닫히면 참조가격을 내린다 -- 다음 포지션이 즉시진입이면
        # REF["px"] 가 None 이라 기존 TP 그대로 돈다.
        if REFTP and state.position is None:
            REF["px"], REF["ret"] = None, None
        today = t.strftime("%Y%m%d") == D
        for o in broker.orders[n0:]:
            if o.success and today:
                orders.append(dict(t=t.isoformat(), side=o.side, sym=o.symbol, qty=int(o.executed_qty), px=float(o.executed_price),
                                   acts=list(r.actions)))
        if today and r.actions:
            acts.append(dict(t=t.isoformat(), a=list(r.actions),
                             mode=p3_stack.position_mode(state) if state.position else "-", regime=state.p3_last_regime))
        t += timedelta(seconds=20)
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
                                                     brk=[e for e in LOG if e["armed_at"][:10].replace("-", "") == D]),
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
