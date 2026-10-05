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
                   snap_reason=str(snap.reason), regime=None, bypass=False)
        _orig_ner = _p3s.note_entry_regime

        def _ner(st, regime, *, now=None):
            rec["regime"] = regime
            if VAR == "E" and regime == _cr.REGIME_CHOP and snap.ok:
                rec["bypass"] = True
                return _orig_ner(st, _cr.REGIME_TREND, now=now)
            return _orig_ner(st, regime, now=now)
        _p3s.note_entry_regime = _ner
        try:
            return _orig_fin(state, outcome=outcome, direction=direction, now=now, session=session, sizing=sizing,
                             presized_chop=presized_chop, bars_3m=bars_3m, signal_detected_at=signal_detected_at,
                             signal_id=signal_id)
        finally:
            _p3s.note_entry_regime = _orig_ner
            ENTRIES.append(rec)
    monkeypatch.setattr(worker, "_finalize_tw2_3slot_entry", _fin)

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

    def px(sym, now):
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

    svc.get_quote = lambda symbol: (None if px(symbol, clock["now"]) is None else QuoteSnapshot(
        symbol=symbol, price=px(symbol, clock["now"]), fetched_at=clock["now"], age_sec=0.0, source="replay", error=None))
    svc.refresh_quotes = lambda *a, **k: None
    Broker = RealFillBroker if FILL == "REAL" else FakeBroker
    broker = Broker(cash=50_000_000.0, quotes={config.LONG_SYMBOL: 10_000.0, config.INVERSE_SYMBOL: 6_000.0})
    state = state_store.default_state()
    state.auto_trade_on = True
    state.mode = "mock"
    state.budget = 10_000_000.0
    strategy_mode.apply(state, strategy_mode.MODE_P3 if VAR in ("A", "E") else strategy_mode.MODE_N1)
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
    Path(os.environ["WK_OUT"]).write_text(json.dumps(dict(D=D, FILL=FILL, VAR=VAR, orders=orders, acts=acts, sig=sig, ex=ex, entries=[e for e in ENTRIES if e["t"][:10].replace("-", "") == D]),
                                                     ensure_ascii=False, indent=1), encoding="utf-8")
