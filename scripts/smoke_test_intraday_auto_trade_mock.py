#!/usr/bin/env python
"""
smoke_test_intraday_auto_trade_mock.py

KIS API 호출 없이 자동매매 전체 흐름을 검증하는 MockBroker 기반 스모크 테스트.
실제 주문은 절대 실행하지 않습니다.

성공 시: MOCK_INTRADAY_AUTO_TRADE_SMOKE_TEST_PASSED 출력
실패 시: 실패 단계 출력 후 exit code 1
"""
import sys
import json
import os
from pathlib import Path
from datetime import datetime

_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_ROOT))

try:
    from dotenv import load_dotenv
    load_dotenv(_ROOT / ".env")
except ImportError:
    pass

FAIL_STEP = None


def fail(step: str, reason: str):
    print(f"\n[FAIL] 단계: {step}")
    print(f"  이유: {reason}")
    sys.exit(1)


# ── Step 0: 임포트 검증 ─────────────────────────────────────────────────────
print("=" * 60)
print("AI-GAP 자동매매 Mock 스모크 테스트")
print("=" * 60)

print("\n[Step 0] 필수 모듈 임포트 검증...")
try:
    from app.services.intraday_auto_trade_service import IntradayAutoTradeService
    from app.strategy.intraday_indicators import (
        calculate_vwap,
        calculate_ema,
        calculate_rsi,
        resample_1m_to_3m,
        detect_bullish_reversal_1m,
        detect_bearish_volume_candle_1m,
        calculate_intraday_high_pullback,
        calculate_volume_ratio,
    )
    print("  OK: app.services.intraday_auto_trade_service")
    print("  OK: app.strategy.intraday_indicators")
except ImportError as e:
    fail("Step 0", f"모듈 임포트 실패: {e}")

print("[Step 0] PASS")


# ── 더미 컴포넌트 정의 ────────────────────────────────────────────────────────

class DummyBroker:
    """실제 API 호출 없이 buy/sell 성공 응답을 반환하는 Broker."""
    mode = "dry_run"

    def __init__(self):
        self.orders = []
        self._counter = 0

    def buy(self, symbol, quantity, price, order_type="limit"):
        self._counter += 1
        oid = f"DUMMY-BUY-{self._counter:03d}"
        self.orders.append({"side": "buy", "symbol": symbol, "qty": quantity, "price": price, "oid": oid})
        return {"success": True, "order_id": oid, "message": "mock_buy_ok"}

    def sell(self, symbol, quantity, price, order_type="limit"):
        self._counter += 1
        oid = f"DUMMY-SELL-{self._counter:03d}"
        self.orders.append({"side": "sell", "symbol": symbol, "qty": quantity, "price": price, "oid": oid})
        return {"success": True, "order_id": oid, "message": "mock_sell_ok"}


class DummyKisClient:
    """실제 API 없이 더미 현재가·1분봉 데이터를 반환하는 KIS 클라이언트."""

    def __init__(self, prices: dict, candles: dict):
        self._prices = prices    # {symbol: float}
        self._candles = candles  # {symbol: list[dict]}

    def get_current_price(self, symbol: str):
        p = self._prices.get(symbol, 10000.0)
        return {"current_price": p, "open": p * 0.98, "high": p * 1.02, "low": p * 0.97}

    def get_minute_candles(self, symbol: str, period_min: int = 1, count: int = 60):
        return self._candles.get(symbol, [])


class MockConfig:
    """IntradayAutoTradeService용 최소 Mock 설정."""

    def __init__(self):
        today = datetime.now().strftime("%Y%m%d")
        self._raw = {
            "safety": {
                "enable_real_buy":     False,
                "enable_real_sell":    False,
                "enable_real_trading": False,
            },
            "intraday_auto_trade": {
                "total_budget":                       3_000_000,
                "max_position_count":                 3,
                "check_interval_seconds":             10,
                "buy_start_time":                     "00:00",
                "buy_end_time":                       "23:59",
                "force_sell_time":                    "23:55",
                "max_total_entries_per_day":          9,
                "max_entries_per_symbol":             3,
                "cooldown_minutes":                   1,
                "allow_breakout_entry_if_no_pullback": True,
                "buy_conditions": {
                    "min_pullback_pct":    -3.8,
                    "max_pullback_pct":    -1.2,
                    "min_volume_ratio":     1.15,
                    "min_rsi":             42.0,
                    "max_rsi":             72.0,
                    "crash_threshold_pct": -5.0,
                },
                "relaxed_buy_conditions": {
                    "min_pullback_pct":  -0.8,
                    "min_volume_ratio":   1.0,
                },
                "sell_conditions": {
                    "stop_loss_pct":         -0.9,
                    "half_take_profit_pct":   1.35,
                    "full_take_profit_pct":   2.2,
                    "trailing_stop_pct":     -1.2,
                },
                "state_file": f"data/state/smoke_test_intraday_state_{today}.json",
                "log_file":   f"data/logs/smoke_test_intraday_log_{today}.csv",
            },
        }


# ── 더미 1분봉 생성 ──────────────────────────────────────────────────────────

def make_buy_candles(base_price: int = 10000) -> list[dict]:
    """
    모든 표준 매수 조건을 만족하는 60개 1분봉 생성 (newest-first).

    조건:
      VWAP < current_price (9980)       → 과거 저가 캔들로 VWAP을 아래로 끌어내림
      -3.8 <= pullback <= -1.2           → 장중고점 10250 대비 -2.63%
      EMA5_3m > EMA20_3m                 → 단기 상승추세
      bullish reversal (idx0 양봉, idx1 음봉)
      volume ratio >= 1.15
      RSI in [42, 72]
    """
    old_first = []

    # 50개 기저 캔들: 9600→9750 완만한 상승 (VWAP 하방 고정)
    for i in range(50):
        p = round(base_price * 0.960 + i * (base_price * 0.015 / 49))
        old_first.append({
            "time":   f"09{i // 60:02d}{i % 60:02d}00",
            "open":   round(p * 0.9988),
            "high":   round(p * 1.0012),
            "low":    round(p * 0.9988),
            "close":  p,
            "volume": 40000,
        })

    # 4개 랠리 캔들: 9850 → 10250 (장중고점 형성)
    rally_closes = [9850, 9980, 10120, 10250]
    for i, p in enumerate(rally_closes):
        old_first.append({
            "time":   f"1005{i:02d}00",
            "open":   round(p * 0.998),
            "high":   10250 if i == 3 else round(p * 1.002),
            "low":    round(p * 0.997),
            "close":  p,
            "volume": 70000 + i * 5000,
        })

    # 4개 눌림 캔들: 10180 → 9980
    pullback_closes = [10180, 10100, 10050, 9980]
    for i, p in enumerate(pullback_closes):
        old_first.append({
            "time":   f"1009{i:02d}00",
            "open":   round(p * 1.001),
            "high":   round(p * 1.003),
            "low":    round(p * 0.997),
            "close":  p,
            "volume": 45000,
        })

    # idx 1: 음봉 (bearish)
    cp = round(base_price * 0.998)  # 9980
    old_first.append({
        "time":   "101300",
        "open":   round(cp * 1.005),   # 10030
        "high":   round(cp * 1.007),   # 10050
        "low":    round(cp * 0.992),   # 9900
        "close":  round(cp * 0.992),   # 9900 bearish
        "volume": 55000,
    })

    # idx 0: 양봉 (bullish reversal) ← 현재가 = 9980
    old_first.append({
        "time":   "101400",
        "open":   round(cp * 0.992),   # 9900
        "high":   round(cp * 1.002),   # 10000
        "low":    round(cp * 0.991),   # 9890
        "close":  cp,                  # 9980 bullish
        "volume": 100000,
    })

    # 총 50+4+4+1+1 = 60개, newest-first
    return list(reversed(old_first))


# ── Step 1: 인디케이터 함수 검증 ────────────────────────────────────────────
print("\n[Step 1] 인디케이터 함수 검증...")

try:
    candles = make_buy_candles()
    assert len(candles) == 60, f"캔들 수 오류: {len(candles)}"

    vwap = calculate_vwap(candles)
    assert vwap > 0, "VWAP=0"

    ema20_list = calculate_ema(candles, 20)
    assert len(ema20_list) == 60, f"EMA20 길이 오류: {len(ema20_list)}"

    rsi = calculate_rsi(candles)
    assert 0 <= rsi <= 100, f"RSI 범위 오류: {rsi:.1f}"

    candles_3m = resample_1m_to_3m(candles)
    assert len(candles_3m) == 20, f"3분봉 수 오류: {len(candles_3m)}"

    pullback = calculate_intraday_high_pullback(9980, 10250)
    assert -3.8 <= pullback <= -1.2, f"눌림률 범위 오류: {pullback:.2f}%"

    vol_ratio = calculate_volume_ratio(candles, 3)
    assert vol_ratio > 0, "거래량비율=0"

    bullish_rev = detect_bullish_reversal_1m(candles)

    ema5_3m = calculate_ema(candles_3m, 5)
    ema20_3m = calculate_ema(candles_3m, 20)

    print(f"  VWAP            = {vwap:.0f}")
    print(f"  RSI             = {rsi:.1f}")
    print(f"  3분봉 수         = {len(candles_3m)}")
    print(f"  눌림률           = {pullback:.2f}%")
    print(f"  거래량 비율       = {vol_ratio:.2f}x")
    print(f"  EMA5_3m[0]      = {ema5_3m[0]:.0f}")
    print(f"  EMA20_3m[0]     = {ema20_3m[0]:.0f}")
    print(f"  EMA5 > EMA20    = {ema5_3m[0] > ema20_3m[0]}")
    print(f"  Bullish Reversal= {bullish_rev}")
except Exception as e:
    fail("Step 1 - 인디케이터 함수", str(e))

print("[Step 1] PASS")


# ── Step 2: 더미 Top3 종목 및 서비스 초기화 ─────────────────────────────────
print("\n[Step 2] IntradayAutoTradeService 초기화 및 Top3 로드...")

SYMBOLS = {
    "005930": {"name": "삼성전자",  "price": 9980},
    "000660": {"name": "SK하이닉스", "price": 15000},
    "035720": {"name": "카카오",    "price": 5000},
}

try:
    candles_by_sym = {}
    prices_by_sym = {}
    for sym, info in SYMBOLS.items():
        bp = info["price"]
        candles_by_sym[sym] = make_buy_candles(base_price=bp)
        prices_by_sym[sym]  = round(bp * 0.998)

    dummy_broker = DummyBroker()
    dummy_kis    = DummyKisClient(prices=prices_by_sym, candles=candles_by_sym)
    cfg          = MockConfig()

    svc = IntradayAutoTradeService(broker=dummy_broker, kis_client=dummy_kis, cfg=cfg)

    top3 = [
        {"symbol": "005930", "name": "삼성전자",  "current_price": 9980,  "final_score": 90, "rank": 1},
        {"symbol": "000660", "name": "SK하이닉스", "current_price": 15000, "final_score": 85, "rank": 2},
        {"symbol": "035720", "name": "카카오",    "current_price": 5000,  "final_score": 80, "rank": 3},
    ]
    svc.load_top3(top3)

    assert len(svc.symbols_state) == 3, f"Top3 로드 실패: {len(svc.symbols_state)}종목"
    for sym in SYMBOLS:
        assert sym in svc.symbols_state, f"{sym} 상태 없음"

    print(f"  종목 수: {len(svc.symbols_state)}")
    for sym, state in svc.symbols_state.items():
        print(f"  {sym} {state['name']}: 예산={state['allocated_budget']:,.0f}원 ({state['allocated_weight']:.1%})")

except Exception as e:
    fail("Step 2 - 서비스 초기화", str(e))

print("[Step 2] PASS")


# ── Step 3: 매수 조건 개별 검증 ─────────────────────────────────────────────
print("\n[Step 3] 매수 조건 (buy flag) 개별 검증...")

try:
    condition_results = {}
    for sym, state in svc.symbols_state.items():
        # 현재가 주입
        price_data = dummy_kis.get_current_price(sym)
        state["current_price"] = price_data["current_price"]

        candles_1m = dummy_kis.get_minute_candles(sym)
        flag, reason = svc._check_buy_flag(sym, state, candles_1m)
        condition_results[sym] = (flag, reason)
        status_str = "OK" if flag else "BLOCKED"
        print(f"  {sym} {state['name']}: {status_str} ({reason})")

    passed_syms = [s for s, (f, _) in condition_results.items() if f]
    if not passed_syms:
        reasons = {s: r for s, (_, r) in condition_results.items()}
        fail("Step 3 - 매수 조건", f"모든 종목 매수 조건 불통과: {reasons}")

    print(f"  매수 조건 통과 종목: {len(passed_syms)}개 {passed_syms}")
except Exception as e:
    fail("Step 3 - 매수 조건", str(e))

print("[Step 3] PASS")


# ── Step 4: run_once() → 매수 실행 확인 ─────────────────────────────────────
print("\n[Step 4] run_once() 실행 → Top3 순차 매수 확인...")

try:
    result = svc.run_once()
    buy_actions = [a for a in result.get("actions", []) if a.get("action") == "buy" and a.get("success")]

    print(f"  전체 액션: {len(result.get('actions', []))}건")
    print(f"  성공 매수: {len(buy_actions)}건")
    for a in buy_actions:
        print(f"    ↑ BUY {a['symbol']} {a.get('quantity')}주 @{a.get('price'):,}원")

    for sym, status in result["symbols"].items():
        print(f"  {sym} 상태: {status}")

    if len(buy_actions) == 0:
        # 매수 조건 통과한 종목이 있었는데 실제 매수 없음
        for sym in passed_syms:
            state = svc.symbols_state[sym]
            print(f"  DEBUG {sym}: status={state['status']}, reason={state.get('last_reason')}")
        fail("Step 4 - run_once 매수", "매수 조건 통과 종목이 있으나 실제 매수 미발생")

except Exception as e:
    fail("Step 4 - run_once", str(e))

print("[Step 4] PASS")


# ── Step 5: 매도 조건 시뮬레이션 ────────────────────────────────────────────
print("\n[Step 5] 매도 조건 시뮬레이션...")

# 매수된 종목 중 첫 번째 선택
bought_sym = next(
    (sym for sym, state in svc.symbols_state.items() if state["status"] in ("HOLDING", "HALF_SOLD")),
    None
)

if bought_sym is None:
    fail("Step 5 - 매도 시뮬레이션 준비", "보유 종목 없음: 매수가 Step 4에서 발생했으나 상태가 HOLDING이 아님")

state = svc.symbols_state[bought_sym]
avg_buy = state["avg_buy_price"]
candles_1m_test = dummy_kis.get_minute_candles(bought_sym)

try:
    sell_tests = {
        "손절 (-0.9%)":      avg_buy * (1 + svc.stop_loss_pct / 100) * 0.999,
        "절반익절 (+1.35%)": avg_buy * (1 + svc.half_tp_pct / 100) * 1.001,
        "전량익절 (+2.2%)":  avg_buy * (1 + svc.full_tp_pct / 100) * 1.001,
    }

    sell_results = {}
    for scenario, test_price in sell_tests.items():
        # 임시 상태 복사본으로 검증
        test_state = dict(state)
        test_state["status"] = "HOLDING"
        test_state["first_take_profit_done"] = False
        test_state["highest_price_after_entry"] = avg_buy * 1.015

        sell_type, sell_reason = svc._check_sell_flag(bought_sym, test_state, candles_1m_test, test_price)
        sell_results[scenario] = (sell_type, sell_reason)
        triggered = "TRIGGERED" if sell_type else "NOT_TRIGGERED"
        print(f"  {scenario}: {triggered} ({sell_type or '-'} / {sell_reason or '-'})")

    # Trailing stop 검증
    test_state2 = dict(state)
    test_state2["status"] = "HOLDING"
    test_state2["first_take_profit_done"] = True
    high_price = avg_buy * 1.025
    test_state2["highest_price_after_entry"] = high_price
    trail_price = high_price * (1 + svc.trailing_stop_pct / 100) * 0.999
    sell_type_trail, sell_reason_trail = svc._check_sell_flag(bought_sym, test_state2, candles_1m_test, trail_price)
    triggered = "TRIGGERED" if sell_type_trail else "NOT_TRIGGERED"
    print(f"  Trailing Stop (-1.2%): {triggered} ({sell_type_trail or '-'} / {sell_reason_trail or '-'})")

    # VWAP 이탈 매도 검증 (current < vwap)
    test_state3 = dict(state)
    test_state3["status"] = "HOLDING"
    test_state3["first_take_profit_done"] = False
    test_state3["highest_price_after_entry"] = avg_buy
    vwap_val = calculate_vwap(candles_1m_test)
    below_vwap_price = vwap_val * 0.996  # VWAP -0.4%
    sell_type_vwap, sell_reason_vwap = svc._check_sell_flag(bought_sym, test_state3, candles_1m_test, below_vwap_price)
    triggered = "TRIGGERED" if sell_type_vwap else "NOT_TRIGGERED"
    print(f"  VWAP 이탈 (VWAP={vwap_val:.0f}, price={below_vwap_price:.0f}): {triggered} ({sell_type_vwap or '-'})")

    # 최소한 손절·익절 조건 2개 이상 작동 확인
    triggered_count = sum(1 for st, _ in sell_results.values() if st)
    if triggered_count < 2:
        fail("Step 5 - 매도 조건", f"매도 조건 {triggered_count}/3만 작동 (최소 2개 필요)")

except Exception as e:
    fail("Step 5 - 매도 조건", str(e))

print("[Step 5] PASS")


# ── Step 6: 실제 매도 실행 확인 ─────────────────────────────────────────────
print("\n[Step 6] 실제 매도 실행 (full_tp) 확인...")

try:
    state_before_sell = svc.symbols_state[bought_sym]
    # full_tp 가격으로 강제 매도 시뮬레이션
    tp_price = state_before_sell["avg_buy_price"] * (1 + svc.full_tp_pct / 100) * 1.001
    sell_result = svc._execute_sell(bought_sym, state_before_sell, "full_tp", int(tp_price))

    assert sell_result.get("success"), f"매도 실패: {sell_result}"
    print(f"  full_tp 매도 성공: {bought_sym} {sell_result.get('quantity')}주 @{sell_result.get('price'):,}원")
    print(f"  상태: {state_before_sell['status']}")

except Exception as e:
    fail("Step 6 - 매도 실행", str(e))

print("[Step 6] PASS")


# ── Step 7: 상태 파일 및 로그 파일 확인 ─────────────────────────────────────
print("\n[Step 7] state 파일·log 파일 생성 확인...")

try:
    state_file = svc.state_file
    log_file   = svc.log_file

    assert state_file.exists(), f"state 파일 미생성: {state_file}"
    assert log_file.exists(),   f"log 파일 미생성: {log_file}"

    # state 파일 JSON 유효성
    with open(state_file, encoding="utf-8") as f:
        state_data = json.load(f)
    assert "symbols" in state_data, "state 파일에 'symbols' 키 없음"
    assert "date" in state_data,    "state 파일에 'date' 키 없음"

    state_size = state_file.stat().st_size
    log_size   = log_file.stat().st_size

    print(f"  state 파일: {state_file.name} ({state_size:,} bytes)")
    print(f"  log 파일:   {log_file.name} ({log_size:,} bytes)")

except Exception as e:
    fail("Step 7 - 파일 확인", str(e))

print("[Step 7] PASS")


# ── Step 8: 실제 주문 미발생 확인 ───────────────────────────────────────────
print("\n[Step 8] 실제 KIS 주문 미발생 확인...")

try:
    # DummyBroker는 실제 HTTP 요청을 하지 않음
    assert isinstance(dummy_broker, DummyBroker), "브로커 타입 오류"
    assert dummy_broker.mode != "real", "실전 모드 브로커 사용됨"

    buy_orders  = [o for o in dummy_broker.orders if o["side"] == "buy"]
    sell_orders = [o for o in dummy_broker.orders if o["side"] == "sell"]

    print(f"  총 더미 매수 주문: {len(buy_orders)}건")
    print(f"  총 더미 매도 주문: {len(sell_orders)}건")
    print(f"  실제 KIS API 호출: 없음 (DummyBroker 사용)")

except Exception as e:
    fail("Step 8 - 실제 주문 미발생", str(e))

print("[Step 8] PASS")


# ── 최종 요약 ───────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("  테스트 요약")
print("=" * 60)
print(f"  broker:      DummyBroker (실제 주문 없음)")
print(f"  kis_client:  DummyKisClient (더미 데이터)")
print(f"  매수 발생:   {len(buy_actions)}건")
print(f"  매도 조건:   손절/절반익절/전량익절/trailing/VWAP 모두 테스트 완료")
print(f"  state 파일:  {svc.state_file.name}")
print(f"  log 파일:    {svc.log_file.name}")
print("=" * 60)
print("\nMOCK_INTRADAY_AUTO_TRADE_SMOKE_TEST_PASSED")

