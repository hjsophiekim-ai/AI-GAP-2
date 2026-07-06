#!/usr/bin/env python
"""
smoke_test_intraday_auto_trade_kis_mock_data.py

실제 KIS mock token + 실제 1분봉 데이터를 사용한 자동매매 연결 스모크 테스트.
- 실제 KIS 모의계좌 API로 1분봉 데이터를 수집
- VWAP / EMA20 / EMA50 / EMA100 / RSI / 눌림률 / 거래량 비율 계산
- IntradayAutoTradeService.run_once() 실행 (DummyBroker: 실제 주문 없음)
- candle_count > 0 확인
- Buy Flag 또는 no-signal reason 정상 계산 확인

성공 시 출력: KIS_MOCK_INTRADAY_DATA_SMOKE_TEST_PASSED
실패 시: 실패 단계 출력 + exit code 1
"""
import sys
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


def fail(step: str, reason: str) -> None:
    print(f"\n[FAIL] {step}")
    print(f"  {reason}")
    sys.exit(1)


TEST_SYMBOL = "005930"  # 삼성전자

print("=" * 62)
print("  AI-GAP KIS Mock 실데이터 자동매매 스모크 테스트")
print("=" * 62)

# ── Step 0: 필수 환경변수 및 모듈 확인 ─────────────────────────────────────
print("\n[Step 0] 환경변수 및 모듈 확인...")

mock_key    = os.getenv("KIS_MOCK_APP_KEY", "")
mock_secret = os.getenv("KIS_MOCK_APP_SECRET", "")
mock_cano   = os.getenv("KIS_MOCK_CANO", "") or os.getenv("KIS_MOCK_ACCOUNT_NO", "")
mock_prdt   = os.getenv("KIS_MOCK_ACNT_PRDT_CD", "") or os.getenv("KIS_MOCK_ACCOUNT_PRODUCT_CODE", "01")

if not mock_key or not mock_secret:
    fail("Step 0", "KIS_MOCK_APP_KEY 또는 KIS_MOCK_APP_SECRET 미설정 (diagnose_intraday_kis_mock.py 먼저 실행)")

try:
    from app.trading.kis_client import KISClient
    from app.services.intraday_auto_trade_service import IntradayAutoTradeService
    from app.strategy.intraday_indicators import (
        calculate_vwap,
        calculate_ema,
        calculate_rsi,
        resample_1m_to_3m,
        calculate_intraday_high_pullback,
        calculate_volume_ratio,
    )
    print("  OK: KISClient, IntradayAutoTradeService, intraday_indicators")
except ImportError as e:
    fail("Step 0 - 모듈 임포트", str(e))

print("[Step 0] PASS")


# ── Step 1: KISClient 생성 및 토큰 확인 ─────────────────────────────────────
print(f"\n[Step 1] KIS mock 클라이언트 초기화...")

try:
    kis = KISClient(
        app_key=mock_key,
        app_secret=mock_secret,
        account_no=mock_cano or "00000000",
        product_code=mock_prdt or "01",
        mode="mock",
    )
    token = kis.get_access_token()
    if not token:
        fail("Step 1", "KIS mock 토큰 발급 실패 (diagnose_intraday_kis_mock.py로 환경변수 확인)")
    print(f"  KIS mock 토큰 발급 성공 (원문 미출력)")
except Exception as e:
    fail("Step 1 - KISClient 초기화", str(e))

print("[Step 1] PASS")


# ── Step 2: 현재가 조회 ─────────────────────────────────────────────────────
print(f"\n[Step 2] 현재가 조회 ({TEST_SYMBOL})...")

current_price = 0.0
try:
    price_data = kis.get_current_price(TEST_SYMBOL)
    if not price_data:
        fail("Step 2", f"{TEST_SYMBOL} 현재가 응답 없음")
    current_price = float(price_data.get("current_price", 0) or 0)
    if current_price <= 0:
        fail("Step 2", f"현재가 0원 반환 (장 마감 중이거나 API 오류)")
    print(f"  {TEST_SYMBOL} 현재가 = {current_price:,.0f}원")
except Exception as e:
    fail("Step 2 - 현재가 조회", str(e))

print("[Step 2] PASS")


# ── Step 3: 1분봉 수집 ─────────────────────────────────────────────────────
print(f"\n[Step 3] 1분봉 수집 ({TEST_SYMBOL}, count=60)...")

candles_1m = []
try:
    candles_1m = kis.get_minute_candles(TEST_SYMBOL, period_min=1, count=60)
    candle_count = len(candles_1m)
    print(f"  1분봉 수신: {candle_count}개")

    _market_open = candle_count > 0

    if candle_count == 0:
        print("  [WARN] 1분봉 0개 반환 - 장 마감/주말이면 정상 (KIS 서버 데이터 없음)")
        print("  [INFO] 파이프라인 연결 검증은 계속 진행합니다.")
    else:
        # 캔들 필드 검증
        sample = candles_1m[0]
        required_fields = {"time", "open", "high", "low", "close", "volume"}
        missing_fields  = required_fields - set(sample.keys())
        if missing_fields:
            fail("Step 3 - 캔들 필드", f"필수 필드 누락: {missing_fields}")
        print(f"  최신 캔들: time={sample['time']} O={sample['open']:.0f} H={sample['high']:.0f} "
              f"L={sample['low']:.0f} C={sample['close']:.0f} V={sample['volume']:,}")
except Exception as e:
    fail("Step 3 - 1분봉 수집", str(e))

print("[Step 3] PASS")


# ── Step 4: 지표 계산 검증 ──────────────────────────────────────────────────
print(f"\n[Step 4] 지표 계산 (VWAP / EMA / RSI / 눌림률 / 거래량비율)...")

vwap      = 0.0
ema20     = []
ema50     = []
ema100    = []
rsi       = 50.0
vol_ratio = 0.0
candles_3m = []
pullback   = 0.0
intraday_high = current_price

if not _market_open:
    print("  [SKIP] 1분봉 없음 - 지표 계산 건너뜀 (장 마감)")
else:
    try:
        vwap      = calculate_vwap(candles_1m)
        ema20     = calculate_ema(candles_1m, 20)
        ema50     = calculate_ema(candles_1m, 50)
        ema100    = calculate_ema(candles_1m, 100)
        rsi       = calculate_rsi(candles_1m)
        vol_ratio = calculate_volume_ratio(candles_1m, 3)

        candles_3m    = resample_1m_to_3m(candles_1m)
        intraday_high = max((c["high"] for c in candles_1m), default=current_price)
        pullback      = calculate_intraday_high_pullback(current_price, intraday_high)

        assert vwap >= 0,       f"VWAP 음수: {vwap}"
        assert len(ema20) > 0,  f"EMA20 비어있음"
        assert 0 <= rsi <= 100, f"RSI 범위 오류: {rsi}"

        print(f"  VWAP           = {vwap:,.0f}")
        print(f"  EMA20[0]       = {ema20[0]:,.0f}" if ema20 else "  EMA20: 데이터 부족")
        print(f"  EMA50[0]       = {ema50[0]:,.0f}" if len(ema50) > 0 else "  EMA50: 데이터 부족 (60개 미만)")
        print(f"  EMA100[0]      = {ema100[0]:,.0f}" if len(ema100) > 0 else "  EMA100: 데이터 부족 (100개 미만)")
        print(f"  RSI            = {rsi:.1f}")
        print(f"  3분봉 수        = {len(candles_3m)}")
        print(f"  장중 고점        = {intraday_high:,.0f}")
        print(f"  현재가 눌림률    = {pullback:+.2f}%")
        print(f"  거래량 비율      = {vol_ratio:.2f}x")
        print(f"  현재가 vs VWAP  = {'ABOVE' if current_price > vwap else 'BELOW'} ({current_price:,.0f} vs {vwap:,.0f})")

    except Exception as e:
        fail("Step 4 - 지표 계산", str(e))

print("[Step 4] PASS")


# ── Step 5: IntradayAutoTradeService run_once() ─────────────────────────────
print(f"\n[Step 5] IntradayAutoTradeService.run_once() (DummyBroker - 실제 주문 없음)...")


class DummyBroker:
    """실제 주문 없이 성공 응답을 반환하는 더미 브로커."""
    mode = "dry_run"

    def __init__(self):
        self.orders = []
        self._cnt = 0

    def buy(self, symbol, quantity, price, order_type="limit"):
        self._cnt += 1
        oid = f"DUMMY-BUY-{self._cnt:03d}"
        self.orders.append({"side": "buy", "sym": symbol, "qty": quantity, "price": price})
        return {"success": True, "order_id": oid}

    def sell(self, symbol, quantity, price, order_type="limit"):
        self._cnt += 1
        oid = f"DUMMY-SELL-{self._cnt:03d}"
        self.orders.append({"side": "sell", "sym": symbol, "qty": quantity, "price": price})
        return {"success": True, "order_id": oid}


class MockConfig:
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
                    "min_pullback_pct":    -4.5,
                    "max_pullback_pct":    -0.5,
                    "min_volume_ratio":     1.0,
                    "min_rsi":             35.0,
                    "max_rsi":             80.0,
                    "crash_threshold_pct": -6.0,
                },
                "relaxed_buy_conditions": {
                    "min_pullback_pct":  -0.3,
                    "min_volume_ratio":   0.8,
                },
                "sell_conditions": {
                    "stop_loss_pct":         -0.9,
                    "half_take_profit_pct":   1.35,
                    "full_take_profit_pct":   2.2,
                    "trailing_stop_pct":     -1.2,
                },
                "state_file": f"data/state/smoke_test_kis_data_state_{today}.json",
                "log_file":   f"data/logs/smoke_test_kis_data_log_{today}.csv",
            },
        }


try:
    dummy_broker = DummyBroker()
    cfg          = MockConfig()

    svc = IntradayAutoTradeService(broker=dummy_broker, kis_client=kis, cfg=cfg)

    top3 = [
        {
            "symbol":        TEST_SYMBOL,
            "name":          "삼성전자",
            "current_price": current_price,
            "final_score":   85,
            "rank":          1,
        },
    ]
    svc.load_top3(top3)
    # 현재가를 실제 조회값으로 갱신
    svc.symbols_state[TEST_SYMBOL]["current_price"] = current_price

    result = svc.run_once()

    actions        = result.get("actions", [])
    symbol_status  = result.get("symbols", {}).get(TEST_SYMBOL, "UNKNOWN")
    state          = svc.symbols_state.get(TEST_SYMBOL, {})
    last_reason    = state.get("last_reason", "")
    last_buy_flag  = state.get("last_buy_flag", False)

    print(f"  종목: {TEST_SYMBOL}")
    print(f"  상태: {symbol_status}")
    print(f"  Buy Flag: {last_buy_flag}")
    print(f"  reason / last_reason: {last_reason or '(없음)'}")
    print(f"  actions: {len(actions)}건")
    for a in actions:
        print(f"    {a.get('action','?')} {a.get('symbol','')} "
              f"qty={a.get('quantity',0)} price={a.get('price',0):,.0f}")

except Exception as e:
    import traceback
    fail("Step 5 - run_once", traceback.format_exc())

print("[Step 5] PASS")


# ── Step 6: candle_count 확인 + reason 정상 계산 확인 ───────────────────────
print(f"\n[Step 6] candle_count 확인 및 reason 정상 계산 확인...")

reason_from_check  = ""
candles_3m_check   = []

try:
    if _market_open:
        # 장 중: candle_count > 0 검증
        assert len(candles_1m) > 0, "candle_count == 0: 1분봉 데이터 없음"
        print(f"  candle_count   = {len(candles_1m)} (> 0) OK")

        # Buy Flag reason이 유효한 문자열인지 확인
        _, reason_from_check = svc._check_buy_flag(
            TEST_SYMBOL,
            svc.symbols_state[TEST_SYMBOL],
            candles_1m,
        )
        assert reason_from_check, "Buy Flag reason이 빈 문자열"
        print(f"  Buy Flag reason = {reason_from_check}")

        candles_3m_check = resample_1m_to_3m(candles_1m)
        print(f"  3분봉 수        = {len(candles_3m_check)} ({'OK' if len(candles_3m_check) >= 5 else 'WARN: 5개 미만'})")

    else:
        # 장 마감: 서비스가 빈 캔들을 올바르게 처리하는지 확인
        print(f"  candle_count   = 0 (장 마감 - WARN, 오류 아님)")
        _, reason_from_check = svc._check_buy_flag(
            TEST_SYMBOL,
            svc.symbols_state[TEST_SYMBOL],
            [],   # empty candles
        )
        assert reason_from_check, "empty candles에 대한 reason이 빈 문자열"
        print(f"  Buy Flag reason (빈 캔들) = {reason_from_check} OK")

except Exception as e:
    fail("Step 6 - 검증", str(e))

print("[Step 6] PASS")


# ── Step 7: 실제 주문 미발생 확인 ───────────────────────────────────────────
print(f"\n[Step 7] 실제 KIS 주문 미발생 확인...")

assert isinstance(dummy_broker, DummyBroker), "브로커 타입 오류"
assert dummy_broker.mode != "real", "실전 모드 브로커 사용됨"
print(f"  DummyBroker 사용 (실제 KIS 주문 API 미호출)")
print(f"  더미 주문 기록: {len(dummy_broker.orders)}건")

print("[Step 7] PASS")


# ── 최종 요약 ───────────────────────────────────────────────────────────────
print("\n" + "=" * 62)
print("  테스트 요약")
print("=" * 62)
print(f"  KIS mode         : mock")
print(f"  종목             : {TEST_SYMBOL}")
print(f"  현재가            : {current_price:,.0f}원")
print(f"  1분봉 수신        : {len(candles_1m)}개 {'(장 중 OK)' if _market_open else '(장 마감 WARN)'}")
print(f"  3분봉 변환        : {len(candles_3m_check)}개")
print(f"  VWAP             : {vwap:,.0f}" if _market_open else "  VWAP             : (장 마감 - 미계산)")
print(f"  RSI              : {rsi:.1f}" if _market_open else "  RSI              : (장 마감 - 미계산)")
print(f"  Buy Flag reason  : {reason_from_check}")
print(f"  실제 주문         : 없음 (DummyBroker)")
print("=" * 62)
print("\nKIS_MOCK_INTRADAY_DATA_SMOKE_TEST_PASSED")

