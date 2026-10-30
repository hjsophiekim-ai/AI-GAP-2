"""P70R SHADOW — N1 오전 손절의 분할손절 가상 기록 (2026-10-30, **기록 전용**).

실제 주문은 지금처럼 100% 전량손절이다. 이 모듈은 주문·수량·시각·청산 판단에 **어떤 영향도
주지 않는다.** 연구(2026-10-29~30 P70R 스크리닝): 14건 중 발동 7건, 이득이 가짜 손절 2건에 몰려
상위 2건 제외 시 음수 -> production 적용 금지, PASS 20건 이상 또는 20영업일 누적 후 재검증.

대상: time-window 오전 래더의 STOP_LOSS(완성 3분봉 종가 기준, N1 −1.3%)로 **실제 전량청산이 체결된**
거래. 전략명 무관. 오후 래더 손절과 B3(P3 스택이 포지션을 관리 중)는 제외한다.

P70R_SHADOW (손절 판정 tick = 실제 SELL 주문 tick 기준)
  PASS        : 현재가가 직전 3분(180초) tick 저점 대비 ≥ +GATE_PCT(0.6%) 반등
  FAIL        : 반등 < 0.6%
  UNAVAILABLE : 직전 3분 tick 이 부족(재시작 직후 등)하거나 현재가가 없다(fresh quote 없음)

가상 변형 (PASS 만, FAIL/UNAVAILABLE 은 A 와 같다)
  A 현행 : 100% 를 실제 SELL 체결가로 (원장 executed_price — ACTUAL_FILL 이면 KIS 실제 평균체결가)
  B 70/30, C 50/50 : 즉시분은 A 와 같은 가격, runner 는 아래 규칙으로 tick 마다 전진(미래 데이터 없음)
runner 규칙 (사전 고정)
  R_TP     : tick 순수익 ≥ +1.5%
  R_NEWLOW : tick 가격 < 직전 3분 저점(손절 시점에 고정)
  R_SL     : 손절 이후 처음 완성되는 3분봉부터, 봉 종가(tick 근사) 순수익 ≤ 손절선(발동한 값, N1 −1.3%)
  R_TIME   : 60분
  체결가는 그 tick 의 현재가(= 매도가 근사). 일자가 바뀌면 마지막 tick 가격으로 R_DAY_END.
모든 손절은 60분 동안 MFE/MAE 를 추적하고, 손절 후 30분 안에 진입가 대비 +1.5% 에 닿으면 가짜 손절(FAKE)로 분류한다.

원장: 신호원장과 같은 폴더의 ``macd2_p70r_shadow.json`` + 체결원장 SELL 행의 p70r_* 3컬럼(읽기 전용 표시용).
이 모듈의 어떤 예외도 worker 가 감싸서 tick 을 깨지 않는다.
"""
from __future__ import annotations

import json
import threading
import time
from collections import deque
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from app.trading.macd2 import config, ledger
from app.trading.macd2.signal_engine import forming_bar_window

PASS = "PASS"
FAIL = "FAIL"
UNAVAILABLE = "UNAVAILABLE"
R_TP, R_NEWLOW, R_SL, R_TIME, R_DAY_END = "R_TP", "R_NEWLOW", "R_SL", "R_TIME", "R_DAY_END"
GATE_PCT = 0.6
RUNNER_TP_PCT = 1.5
FAKE_TP_PCT = 1.5
FAKE_WINDOW_MIN = 30
TRACK_MIN = 60
LOOKBACK_SEC = 180
MIN_COVER_SEC = 150          # 직전 3분 창의 시작 쪽 30초 안에 tick 이 하나는 있어야 한다
MIN_TICKS = 5
SPLITS = {"7030": 0.7, "5050": 0.5}
FEE_RATE = 36.4e-6           # 원장과 같은 편도 수수료율
_FLUSH_SEC = 60
#: 손절봉 완성 -> 실제 SELL 요청 지연 구간(초). 10/29 실거래는 판정 후 ~70초 뒤 매도였다.
DELAY_BUCKETS = ((10.0, "<=10s"), (30.0, "10-30s"), (60.0, "30-60s"), (float("inf"), ">60s"))

_LOCK = threading.Lock()
_TICKS: dict[str, deque] = {}
_OPEN_HINT: Optional[bool] = None      # None = 모름(첫 호출에 파일 확인)
_CACHE: Optional[dict[str, dict]] = None   # 프로세스 안에서는 이것이 기준(tick 관측값이 파일 flush 전에 사라지지 않게)


def is_enabled() -> bool:
    return bool(getattr(config, "P70R_SHADOW_ENABLED", True))


def ledger_path() -> Path:
    return Path(ledger.SIGNAL_LEDGER_PATH).parent / config.P70R_SHADOW_FILENAME


def reset_for_tests() -> None:
    global _OPEN_HINT, _CACHE
    with _LOCK:
        _TICKS.clear()
        _OPEN_HINT = None
        _CACHE = None


def delay_bucket(sec: Optional[float]) -> str:
    if sec is None:
        return "UNKNOWN"
    for edge, label in DELAY_BUCKETS:
        if sec <= edge:
            return label
    return "UNKNOWN"


def _sec_between(a: Optional[str], b: Optional[str]) -> Optional[float]:
    try:
        return round((datetime.fromisoformat(str(b)) - datetime.fromisoformat(str(a))).total_seconds(), 3)
    except (TypeError, ValueError):
        return None


def _net_pct(entry: float, price: float) -> float:
    return ((price - entry) - (entry + price) * FEE_RATE) / entry * 100.0


def _rows_cached() -> dict[str, dict]:
    """프로세스 첫 접근에만 파일을 읽는다(재시작 복원). 이후는 메모리가 기준이다."""
    global _CACHE
    if _CACHE is None:
        _CACHE = _load()
    return _CACHE


def _load() -> dict[str, dict]:
    p = ledger_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return {}


def _save(rows: dict[str, dict]) -> None:
    ledger._assert_safe_to_write_ledger()
    p = ledger_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for i in range(5):                      # Windows: 다른 프로세스가 잠깐 잡고 있으면 재시도
        try:
            tmp.replace(p)
            return
        except PermissionError:
            if i == 4:
                raise
            time.sleep(0.05)


# ── tick 관측 (매 tick, 두 ETF) ───────────────────────────────────────────
def observe(now: datetime, quotes: dict) -> None:
    """fresh quote 만 들어온다(worker._fresh_quote_prices). 최근 5분만 메모리에 둔다."""
    cutoff = now - timedelta(seconds=LOOKBACK_SEC + 120)
    with _LOCK:
        for sym in (config.LONG_SYMBOL, config.INVERSE_SYMBOL):
            px = quotes.get(sym)
            dq = _TICKS.setdefault(sym, deque())
            if px and float(px) > 0:
                dq.append((now, float(px)))
            while dq and dq[0][0] < cutoff:
                dq.popleft()


def gate(symbol: str, now: datetime, price: Optional[float]) -> dict[str, Any]:
    """손절 순간의 게이트. 직전 3분 저점은 [now−180s, now] tick(현재가 포함)의 최저가."""
    with _LOCK:
        window = [(t, p) for t, p in _TICKS.get(symbol, ()) if t >= now - timedelta(seconds=LOOKBACK_SEC) and t <= now]
    if price is None or float(price) <= 0:
        return {"status": UNAVAILABLE, "low3": None, "rebound_pct": None, "why": "NO_FRESH_QUOTE"}
    pts = [p for _, p in window] + [float(price)]
    covered = bool(window) and window[0][0] <= now - timedelta(seconds=MIN_COVER_SEC)
    if not covered or len(window) < MIN_TICKS:
        return {"status": UNAVAILABLE, "low3": None, "rebound_pct": None,
                "why": f"TICKS_INSUFFICIENT:{len(window)}"}
    low3 = min(pts)
    reb = (float(price) / low3 - 1.0) * 100.0
    return {"status": PASS if reb >= GATE_PCT else FAIL, "low3": low3, "rebound_pct": round(reb, 4), "why": ""}


# ── 손절 체결 직후 1회 ─────────────────────────────────────────────────────
def record_stop(*, now: datetime, symbol: str, qty: int, entry_price: float, stop_pct: float,
                bar_net_pct: float, decision_price: Optional[float], gate_info: dict, outcome,
                exit_reason: str, strategy_mode: str = "") -> Optional[dict]:
    """실제 전량 손절이 EXECUTED 된 직후. 같은 SELL 주문번호는 한 번만 기록한다."""
    global _OPEN_HINT
    sell = getattr(outcome, "sell_result", None)
    order_id = str(getattr(sell, "order_id", "") or "")
    key = order_id or f"{symbol}:{now.isoformat()}"
    ts = dict(getattr(outcome, "timestamps", None) or {})
    with _LOCK:
        rows = _rows_cached()
        if key in rows:
            return None
        row = {
            "key": key, "order_id": order_id, "trading_date": now.strftime("%Y%m%d"), "symbol": symbol,
            "strategy_mode": strategy_mode, "exit_reason": exit_reason, "qty": int(qty),
            "entry_price": float(entry_price), "stop_pct": float(stop_pct), "bar_net_pct": round(float(bar_net_pct), 4),
            "decision_at": now.isoformat(), "decision_price": decision_price,
            "sell_requested_at": ts.get("sell_requested_at", ""), "sell_confirmed_at": ts.get("sell_confirmed_at", ""),
            "sell_reconciled_at": ts.get("sell_reconciled_at", ""),
            # 지연 계측: 손절봉 완성(= 지금 형성 중인 봉의 시작) -> 판정 tick -> 실제 SELL 요청.
            # 판정과 요청은 같은 tick 이라 보통 수 ms 이고, 큰 지연은 봉 완성 -> 판정 구간에서 생긴다.
            "stop_bar_end_at": forming_bar_window(now)[0].isoformat(), "stop_decision_at": now.isoformat(),
            "decision_to_request_ms": None, "bar_end_to_decision_sec": None, "bar_end_to_request_sec": None,
            "delay_bucket": "UNKNOWN",
            "P70R_SHADOW": gate_info.get("status", UNAVAILABLE), "gate_why": gate_info.get("why", ""),
            "low3": gate_info.get("low3"), "rebound_pct": gate_info.get("rebound_pct"),
            # 실제 SELL 가격은 finalize 때 원장에서 다시 읽는다(그 사이 ACTUAL_FILL 로 갱신될 수 있다).
            "sell_price": None, "price_source": "", "fill_at": "",
            "runner_exit_at": "", "runner_exit_price": None, "runner_exit_reason": "",
            "runner_last_bar_ts": forming_bar_window(now)[0].isoformat(),
            "last_tick_at": now.isoformat(), "last_price": decision_price,
            "mfe_pct": None, "mae_pct": None, "fake_stop": None, "fake_hit_at": "",
            "A_pct": None, "B7030_pct": None, "C5050_pct": None,
            "delta_7030_pct": None, "delta_5050_pct": None, "delta_7030_krw": None, "delta_5050_krw": None,
            "done": False, "last_flush": now.isoformat(),
        }
        d2r = _sec_between(row["stop_decision_at"], row["sell_requested_at"])
        row["decision_to_request_ms"] = None if d2r is None else round(d2r * 1000.0, 1)
        row["bar_end_to_decision_sec"] = _sec_between(row["stop_bar_end_at"], row["stop_decision_at"])
        row["bar_end_to_request_sec"] = _sec_between(row["stop_bar_end_at"], row["sell_requested_at"])
        row["delay_bucket"] = delay_bucket(row["bar_end_to_request_sec"])
        rows[key] = row
        _save(rows)
        _OPEN_HINT = True
        return dict(row)


def _ledger_sell(order_id: str) -> dict:
    if not order_id:
        return {}
    for r in ledger.load_execution_ledger(limit=0):
        if str(r.get("order_id") or "") == order_id and str(r.get("side") or "").upper() == "SELL":
            return r
    return {}


def _finalize(r: dict) -> None:
    led = _ledger_sell(r["order_id"])
    try:
        sell_px = float(led.get("executed_price") or 0.0) or None
    except (TypeError, ValueError):
        sell_px = None
    sell_px = sell_px or r.get("decision_price")
    r["sell_price"] = sell_px
    r["price_source"] = str(led.get("price_source") or "")
    r["fill_at"] = str(led.get("fill_last_at") or led.get("timestamp") or "")
    entry = float(r["entry_price"])
    if not sell_px or entry <= 0:
        r["done"] = True
        return
    a = _net_pct(entry, float(sell_px))
    r["A_pct"] = round(a, 4)
    runner = None
    if r["P70R_SHADOW"] == PASS and r.get("runner_exit_price"):
        runner = _net_pct(entry, float(r["runner_exit_price"]))
    for tag, frac in SPLITS.items():
        v = (frac * a + (1 - frac) * runner) if runner is not None else a
        r["B7030_pct" if tag == "7030" else "C5050_pct"] = round(v, 4)
        d = v - a
        r[f"delta_{tag}_pct"] = round(d, 4)
        r[f"delta_{tag}_krw"] = round(d / 100.0 * entry * int(r["qty"]), 0)
    r["done"] = True
    fields = {"p70r_shadow": r["P70R_SHADOW"], "p70r_delta_7030_pct": r["delta_7030_pct"],
              "p70r_sell_delay_sec": r.get("bar_end_to_request_sec"),
              "p70r_delta_5050_pct": r["delta_5050_pct"]}
    try:
        ledger.record_p70r_fields(r["order_id"], fields)
    except Exception:
        pass


def update(now: datetime, quotes: dict) -> None:
    """열린 행의 runner / MFE·MAE / 가짜손절을 한 tick 전진. 열린 행이 없으면 파일을 읽지 않는다."""
    global _OPEN_HINT
    if _OPEN_HINT is False:
        return
    with _LOCK:
        rows = _rows_cached()
        open_rows = [r for r in rows.values() if not r.get("done")]
        if not open_rows:
            _OPEN_HINT = False
            return
        changed = False
        for r in open_rows:
            dec_at = datetime.fromisoformat(r["decision_at"])
            entry = float(r["entry_price"])
            px = quotes.get(r["symbol"])
            if now.date() != dec_at.date():
                # 일자 변경: 열린 runner 는 마지막 관측가로 닫는다.
                if r["P70R_SHADOW"] == PASS and not r["runner_exit_reason"]:
                    r["runner_exit_reason"], r["runner_exit_at"] = R_DAY_END, r["last_tick_at"]
                    r["runner_exit_price"] = r.get("last_price")
                if r["fake_stop"] is None:
                    r["fake_stop"] = False
                _finalize(r); changed = True
                continue
            if now <= dec_at:
                continue
            if px and float(px) > 0:
                px = float(px)
                n = _net_pct(entry, px)
                if r["mfe_pct"] is None or n > r["mfe_pct"]:
                    r["mfe_pct"] = round(n, 4)
                if r["mae_pct"] is None or n < r["mae_pct"]:
                    r["mae_pct"] = round(n, 4)
                if r["fake_stop"] is None and now <= dec_at + timedelta(minutes=FAKE_WINDOW_MIN) and n >= FAKE_TP_PCT:
                    r["fake_stop"], r["fake_hit_at"], changed = True, now.isoformat(), True
                if r["P70R_SHADOW"] == PASS and not r["runner_exit_reason"]:
                    reason = None
                    bar_start = forming_bar_window(now)[0].isoformat()
                    if bar_start != r["runner_last_bar_ts"]:
                        # 손절 판정 tick 이 속한 봉(=손절 이후 첫 봉)부터, 봉이 막 완성될 때마다
                        # 그 봉의 마지막 관측가(last_price)를 종가로 본다 -- worker 의 손절봉 근사와 같다.
                        prev_close = r.get("last_price")
                        r["runner_last_bar_ts"] = bar_start
                        if prev_close and _net_pct(entry, float(prev_close)) <= float(r["stop_pct"]):
                            reason = R_SL
                    if reason is None and n >= RUNNER_TP_PCT:
                        reason = R_TP
                    elif reason is None and r.get("low3") and px < float(r["low3"]):
                        reason = R_NEWLOW
                    elif reason is None and now >= dec_at + timedelta(minutes=TRACK_MIN):
                        reason = R_TIME
                    if reason:
                        r["runner_exit_reason"], r["runner_exit_at"], r["runner_exit_price"] = reason, now.isoformat(), px
                        changed = True
                r["last_tick_at"], r["last_price"] = now.isoformat(), px
            if now >= dec_at + timedelta(minutes=TRACK_MIN):
                if r["P70R_SHADOW"] == PASS and not r["runner_exit_reason"]:
                    r["runner_exit_reason"], r["runner_exit_at"] = R_TIME, r["last_tick_at"]
                    r["runner_exit_price"] = r.get("last_price")
                if r["fake_stop"] is None:
                    r["fake_stop"] = False
                _finalize(r); changed = True
            elif (now - datetime.fromisoformat(r["last_flush"])).total_seconds() >= _FLUSH_SEC:
                changed = True
            if changed:
                r["last_flush"] = now.isoformat()
        if changed:
            _save(rows)


def rows() -> list[dict]:
    with _LOCK:
        return sorted((dict(r) for r in _rows_cached().values()), key=lambda r: r.get("decision_at") or "")


def summary(all_rows: Optional[list[dict]] = None) -> dict[str, Any]:
    """UI/연구 집계. 판정은 하지 않는다 — 재검증 조건 충족 여부만 알려준다."""
    rs = all_rows if all_rows is not None else rows()
    done = [r for r in rs if r.get("done") and r.get("A_pct") is not None]
    p = [r for r in done if r.get("P70R_SHADOW") == PASS]
    days = sorted({r["trading_date"] for r in rs})
    out: dict[str, Any] = {
        "days": len(days), "stops": len(rs), "done": len(done),
        "pass": sum(1 for r in rs if r.get("P70R_SHADOW") == PASS),
        "fail": sum(1 for r in rs if r.get("P70R_SHADOW") == FAIL),
        "unavailable": sum(1 for r in rs if r.get("P70R_SHADOW") == UNAVAILABLE),
        "pass_fake": sum(1 for r in p if r.get("fake_stop")), "pass_real": sum(1 for r in p if r.get("fake_stop") is False),
        "ready_for_review": (len(days) >= int(config.P70R_SHADOW_REVIEW_MIN_DAYS)
                             or sum(1 for r in rs if r.get("P70R_SHADOW") == PASS) >= int(config.P70R_SHADOW_REVIEW_MIN_PASS)),
    }
    for tag, col in (("A", "A_pct"), ("7030", "B7030_pct"), ("5050", "C5050_pct")):
        by_day: dict[str, float] = {}
        for r in done:
            by_day[r["trading_date"]] = by_day.get(r["trading_date"], 0.0) + float(r[col])
        vals = [float(r[col]) for r in done]
        g = sum(v for v in vals if v > 0); l_ = -sum(v for v in vals if v < 0)
        eq = peak = mdd = 0.0
        for d in sorted(by_day):
            eq += by_day[d]; peak = max(peak, eq); mdd = min(mdd, eq - peak)
        out[f"{tag}_total_pct"] = round(sum(vals), 3)
        out[f"{tag}_pf"] = round(g / l_, 3) if l_ else None
        out[f"{tag}_mdd_pct"] = round(mdd, 3)
        out[f"{tag}_worst_day_pct"] = round(min(by_day.values()), 3) if by_day else None
        if tag != "A":
            ds = sorted((float(r[f"delta_{tag}_pct"]) for r in done), reverse=True)
            out[f"delta_{tag}_pct"] = round(sum(ds), 3)
            out[f"delta_{tag}_krw"] = round(sum(float(r[f"delta_{tag}_krw"] or 0) for r in done), 0)
            out[f"delta_{tag}_ex_top1_pct"] = round(sum(ds[1:]), 3) if ds else None
            out[f"delta_{tag}_ex_top2_pct"] = round(sum(ds[2:]), 3) if ds else None
            out[f"delta_{tag}_real_pct"] = round(sum(float(r[f"delta_{tag}_pct"]) for r in p if r.get("fake_stop") is False), 3)
            out[f"delta_{tag}_fake_pct"] = round(sum(float(r[f"delta_{tag}_pct"]) for r in p if r.get("fake_stop")), 3)
    # 손절봉 완성 -> SELL 요청 지연 구간별 PASS율·성과
    by_delay: dict[str, dict] = {}
    for _, label in (*DELAY_BUCKETS, (None, "UNKNOWN")):
        g_all = [r for r in rs if r.get("delay_bucket", "UNKNOWN") == label]
        if not g_all:
            continue
        g_done = [r for r in done if r.get("delay_bucket", "UNKNOWN") == label]
        n_pass = sum(1 for r in g_all if r.get("P70R_SHADOW") == PASS)
        by_delay[label] = {
            "stops": len(g_all), "pass": n_pass, "pass_rate": round(n_pass / len(g_all), 3),
            "fake": sum(1 for r in g_done if r.get("fake_stop")),
            "A_total_pct": round(sum(float(r["A_pct"]) for r in g_done), 3),
            "delta_7030_pct": round(sum(float(r["delta_7030_pct"]) for r in g_done), 3),
            "delta_5050_pct": round(sum(float(r["delta_5050_pct"]) for r in g_done), 3),
        }
    out["by_delay"] = by_delay
    return out
