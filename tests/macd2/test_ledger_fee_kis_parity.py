"""거래원장 `fee` 컬럼 ↔ KIS 실측 매매비용 1원 단위 parity — 2026-09-18.

무엇이 남아 있었나
------------------
2026-09-17 의 수정(b8b321e)은 `_record_leg` 의 **매도 분기만** 실현비용으로
옮겼다. 매수 분기는 전략 판정용 요율(0.00015)을 쓰는 `compute_trade_cost` 에
그대로 남아 있었고, 오히려 그 커밋이 `round()` 를 붙이는 바람에

    98,155 x 0.00015 = 14.72  ->  **15원**   (KIS 실제 4원)

로 깔끔하게 15원이 찍혔다. "수수료를 고쳤는데 원장에 계속 15원이 나온다"의
정체가 이것이다. broker-direct 경로 2곳(`append_broker_direct_execution`,
`append_broker_direct_fill`)도 같은 요율에 남아 있었고, reconcile 매수 백필은
수수료를 아예 0으로 적어 일일 집계가 KIS 총비용보다 작게 나왔다.

이 파일이 고정하는 것
--------------------
* 매수/매도 **양쪽** 레그의 원장 `fee` 가 KIS 실측과 1원 단위로 같다.
* 우선순위가 매수/매도에 동일하다 — KIS 가 준 제비용 > realized_fee_rate,
  전략 요율은 어느 원장 경로에도 들어오지 않는다.
* 일일 집계 수수료 합이 KIS 총 매매비용과 같다.
* 전략 판정용 비용모델(`compute_net_pnl`/`compute_trade_cost`/`_net_return_pct`)
  값은 **한 자리도 변하지 않았다** — 손절/익절 판정선과 백테스트가 거기 묶여
  있다(2026-09-17 사용자 결정: 2단계 별도 브랜치에서만 건드린다).

fixture 는 tests/macd2/test_kis_realized_parity.py 와 같은 2026-09-17 실계좌
데이터다(KIS inquire-daily-ccld / TTTC8715R 원본).
"""
from __future__ import annotations

import pytest

from app.trading.macd2 import config, ledger, order_executor
from app.trading.macd2.broker_adapter import BrokerOrderResult
from app.trading.trading_cost_engine import TradeCostEngine

SYMBOL = config.INVERSE_SYMBOL   # 0197X0 — 그날 실제로 거래된 인버스

# ── 2026-09-17 실계좌 (KIS 화면 기준) ──────────────────────────────────────
#
# 그날은 레그가 4개였다. 원장은 체결"금액"을 단가 x 수량으로 적으므로, 실제
# 체결금액과 맞추려면 금액에서 역산한 단가를 써야 한다(부분체결이 섞인 16주
# 매수의 실제 금액은 98,155원이고 KIS 표기 평균단가 6,134 로는 98,144원이라
# 11원 어긋난다 — 2026-09-17 원인 ③).
BUY1_QTY, BUY1_AMOUNT = 1, 6_220.0
SELL1_QTY, SELL1_AMOUNT = 1, 6_100.0
BUY16_QTY, BUY16_AMOUNT = 16, 98_155.0
SELL16_QTY, SELL16_AMOUNT = 16, 100_000.0

BUY16_PRICE = BUY16_AMOUNT / BUY16_QTY      # 6,134.6875
SELL16_PRICE = SELL16_AMOUNT / SELL16_QTY   # 6,250.0

#: KIS 계좌 화면(TTTC8715R)이 보여준 값 — 여기에 1원 단위로 맞춘다.
KIS_BUY_FEE_16 = 4
KIS_SELL_FEE_16 = 4
KIS_FEE_TOTAL = 8           # tot_fee (17주 전체, 1주 레그들은 0원)
KIS_REALIZED_PNL_16 = 1_837  # 100,000 - 98,155 - 8
KIS_REALIZED_PNL_DAY = 1_717  # tot_rlzt_pfls (16주 +1,837, 1주 -120)

#: 고쳐지기 전 원장이 적던 값 — 회귀 감시용
LEGACY_BUY_FEE_15 = 15


def _order_result(order_id: str, side: str, qty: int, price: float, raw=None) -> BrokerOrderResult:
    return BrokerOrderResult(
        success=True, order_id=order_id, symbol=SYMBOL, side=side,
        requested_qty=qty, executed_qty=qty, executed_price=price,
        message="", raw=raw if raw is not None else {},
    )


def _record(order_id: str, side: str, qty: int, price: float, *, entry_price: float,
            position_before: int, position_after: int, at: str, raw=None) -> dict:
    """실제 체결 기록 경로(order_executor._record_leg)를 그대로 태우고 그 행을 돌려준다."""
    order_executor._record_leg(
        broker_mode="real", signal_id=f"20260917_{order_id}", symbol=SYMBOL, side=side,
        qty=qty, price=price, position_before=position_before, position_after=position_after,
        exit_reason=("" if side == "BUY" else config.EXIT_TW_STOP_LOSS),
        order_result=_order_result(order_id, side, qty, price, raw=raw),
        entry_price=entry_price, confirmed_at=at,
    )
    return [r for r in ledger.load_execution_ledger() if r["order_id"] == order_id][0]


# ── 1. 매수 레그: 15원이 아니라 4원 ────────────────────────────────────────

def test_buy_leg_fee_matches_kis_and_is_no_longer_fifteen_won():
    """사용자가 본 바로 그 숫자 — 16주 매수의 원장 수수료."""
    row = _record("0015566900", "BUY", BUY16_QTY, BUY16_PRICE,
                  entry_price=BUY16_PRICE, position_before=0, position_after=BUY16_QTY,
                  at="2026-09-17T14:20:41+09:00")

    assert float(row["fee"]) == KIS_BUY_FEE_16
    assert float(row["fee"]) != LEGACY_BUY_FEE_15, (
        "전략 판정용 요율(0.00015)로 되돌아갔다 — 98,155 x 0.00015 = 14.72 -> 15원"
    )
    # 매수 레그에는 손익이 없다(청산 때 실현된다) — 종전과 같다.
    assert float(row["gross_pnl"]) == 0.0
    assert float(row["net_pnl"]) == 0.0
    assert float(row["slippage"]) == 0.0


def test_small_leg_fee_rounds_to_zero_exactly_as_kis_does():
    """1주(6,220원) 레그는 KIS 도 0원을 뗐다 — 레그별 원 단위 반올림."""
    row = _record("0007472700", "BUY", BUY1_QTY, BUY1_AMOUNT,
                  entry_price=BUY1_AMOUNT, position_before=0, position_after=BUY1_QTY,
                  at="2026-09-17T09:55:04+09:00")
    assert float(row["fee"]) == 0.0


# ── 2. 매도 레그: 수수료 4원 + 실현손익 1,837원 ────────────────────────────

def test_sell_leg_fee_and_realized_pnl_match_the_kis_account():
    row = _record("0016650200", "SELL", SELL16_QTY, SELL16_PRICE,
                  entry_price=BUY16_PRICE, position_before=SELL16_QTY, position_after=0,
                  at="2026-09-17T15:00:08+09:00")

    assert float(row["fee"]) == KIS_SELL_FEE_16
    assert float(row["gross_pnl"]) == SELL16_AMOUNT - BUY16_AMOUNT   # 1,845
    assert float(row["net_pnl"]) == KIS_REALIZED_PNL_16              # 1,837
    assert float(row["slippage"]) == 0.0, "체결이 끝난 거래에 예상 슬리피지를 다시 빼지 않는다"


def test_round_trip_fee_total_is_eight_won():
    """매수 4 + 매도 4 = 8원. 이 왕복 하나가 KIS 총 매매비용 전부다."""
    buy = _record("0015566900", "BUY", BUY16_QTY, BUY16_PRICE,
                  entry_price=BUY16_PRICE, position_before=0, position_after=BUY16_QTY,
                  at="2026-09-17T14:20:41+09:00")
    sell = _record("0016650200", "SELL", SELL16_QTY, SELL16_PRICE,
                   entry_price=BUY16_PRICE, position_before=SELL16_QTY, position_after=0,
                   at="2026-09-17T15:00:08+09:00")

    assert float(buy["fee"]) + float(sell["fee"]) == KIS_FEE_TOTAL
    assert float(sell["net_pnl"]) == KIS_REALIZED_PNL_16


# ── 3. 일일 집계가 KIS 총비용과 같은가 ─────────────────────────────────────

def _record_the_whole_20260917_day() -> None:
    """그날 실제로 있었던 4개 레그를 시간순으로 기록한다."""
    _record("0007472700", "BUY", BUY1_QTY, BUY1_AMOUNT, entry_price=BUY1_AMOUNT,
            position_before=0, position_after=BUY1_QTY, at="2026-09-17T09:55:04+09:00")
    _record("0015360300", "SELL", SELL1_QTY, SELL1_AMOUNT, entry_price=BUY1_AMOUNT,
            position_before=BUY1_QTY, position_after=0, at="2026-09-17T14:12:45+09:00")
    _record("0015566900", "BUY", BUY16_QTY, BUY16_PRICE, entry_price=BUY16_PRICE,
            position_before=0, position_after=BUY16_QTY, at="2026-09-17T14:20:41+09:00")
    _record("0016650200", "SELL", SELL16_QTY, SELL16_PRICE, entry_price=BUY16_PRICE,
            position_before=SELL16_QTY, position_after=0, at="2026-09-17T15:00:08+09:00")


def test_daily_total_fee_equals_the_kis_account_total_cost():
    """`summarize_daily_trading` 의 total_cost 는 fee 컬럼 단순합이다 — 매수
    레그가 전략 요율에 남아 있으면 여기서 곧바로 어긋난다."""
    _record_the_whole_20260917_day()

    summary = ledger.summarize_daily_trading("20260917")

    assert summary["buy_count"] == 2 and summary["sell_count"] == 2
    assert summary["total_cost"] == KIS_FEE_TOTAL          # KIS tot_fee = 8
    assert summary["net_pnl"] == KIS_REALIZED_PNL_DAY      # KIS tot_rlzt_pfls = 1,717


def test_daily_total_would_have_been_wrong_under_the_strategy_rate():
    """회귀 감시: 전략 요율이었다면 총비용이 KIS 8원의 4배 언저리가 된다."""
    e = TradeCostEngine()
    strategy_total = sum(
        round(e.compute_trade_cost(SYMBOL, side, amount, 1, order_type="market")["fee"])
        for side, amount in (
            ("BUY", BUY1_AMOUNT), ("SELL", SELL1_AMOUNT),
            ("BUY", BUY16_AMOUNT), ("SELL", SELL16_AMOUNT),
        )
    )
    assert strategy_total == 32    # 1 + 1 + 15 + 15 — KIS 는 8원이었다
    assert strategy_total != KIS_FEE_TOTAL


# ── 4. KIS 가 실제 비용을 주면 언제나 그 값이 이긴다 (매수/매도 동일) ──────

@pytest.mark.parametrize("side", ["BUY", "SELL"])
def test_kis_reported_leg_cost_beats_our_rate_on_both_sides(side):
    """우선순위 1순위 — 브로커가 아는 숫자를 우리가 추측하지 않는다."""
    row = _record(f"kis-cost-{side}", side, BUY16_QTY, BUY16_PRICE,
                  entry_price=BUY16_PRICE, position_before=(0 if side == "BUY" else BUY16_QTY),
                  position_after=(BUY16_QTY if side == "BUY" else 0),
                  at="2026-09-17T14:20:41+09:00",
                  raw={"ODNO": "x", "prsm_tlex": "37"})

    assert float(row["fee"]) == 37.0, "KIS 가 알려준 제비용이 요율 추정보다 우선해야 한다"


def test_account_total_cost_field_is_never_mistaken_for_a_single_leg():
    """`prsm_tlex_smtl`(당일 계좌 합계)을 레그 비용으로 붙이면 비용이 부풀려진다."""
    from app.trading.kis_realized import kis_reported_leg_cost

    assert kis_reported_leg_cost({"prsm_tlex_smtl": "8"}) is None
    assert kis_reported_leg_cost({"tot_fee": "8"}) is None
    assert kis_reported_leg_cost({"prsm_tlex": "8"}) == 8.0


def test_our_own_stale_fee_column_is_never_read_back_as_kis_truth():
    """원장 행에도 `fee` 가 있지만 그건 우리가 계산한 값이다 — 2026-09-17 이전의
    15원짜리 추정치가 "KIS 실측"으로 둔갑하면 안 된다."""
    from app.trading.kis_realized import kis_reported_leg_cost

    assert kis_reported_leg_cost({"fee": "15"}) is None


# ── 5. 나머지 원장 기록 경로 전수 ─────────────────────────────────────────

class _FakeQuoteBroker:
    def __init__(self, price):
        self._price = price

    def get_current_price(self, symbol):
        del symbol
        return self._price


class _FakeDirectOrderResult:
    """append_broker_direct_execution 이 실제로 읽는 속성만 가진 stand-in
    (브로커 계층이 넘기는 app.models.OrderResult 는 `quantity` 를 쓴다 —
    macd2 의 BrokerOrderResult 와 필드 이름이 다르다)."""

    def __init__(self, *, order_id, side, quantity, price, raw, mode="real"):
        self.success = True
        self.order_id = order_id
        self.symbol = SYMBOL
        self.side = side
        self.quantity = quantity
        self.price = price
        self.raw = raw
        self.timestamp = ""
        self.mode = mode


def test_broker_direct_execution_records_realized_cost_not_strategy_cost():
    """워커 밖에서 나간 주문(수동 주문/브로커 계층 직접 기록)."""
    order_result = _FakeDirectOrderResult(
        order_id="direct-16", side="SELL", quantity=BUY16_QTY, price=0.0,
        raw={"ODNO": "direct-16", "ORD_TMD": "150008"})

    assert ledger.append_broker_direct_execution(
        order_result, broker=_FakeQuoteBroker(SELL16_PRICE)) is True

    row = [r for r in ledger.load_execution_ledger() if r["order_id"] == "direct-16"][0]
    assert float(row["fee"]) == KIS_SELL_FEE_16
    assert float(row["fee"]) != LEGACY_BUY_FEE_15


def test_broker_direct_fill_backfill_prefers_the_kis_fill_amount():
    """체결내역 백필 — KIS 가 준 체결금액(tot_ccld_amt)이 단가 x 수량보다 우선."""
    fill = {
        "order_id": "fill-16", "symbol": SYMBOL, "side": "BUY",
        "quantity": BUY16_QTY, "price": 6_134.0,      # KIS 표기 평균단가
        "amount": BUY16_AMOUNT,                        # 실제 체결금액 98,155
        "timestamp": "20260917142041",
    }
    assert ledger.append_broker_direct_fill(fill, mode="real") is True

    row = [r for r in ledger.load_execution_ledger() if r["order_id"] == "fill-16"][0]
    assert float(row["fee"]) == KIS_BUY_FEE_16


def test_reconcile_backfill_buy_fee_counts_toward_the_daily_total():
    """2026-09-18 변경: reconcile 로 발견된 매수의 수수료를 0 으로 두면 일일
    집계가 KIS 총비용보다 작아진다. 수량/평단가는 브로커가 확인해 준 값이므로
    금액은 알려져 있고, KIS 는 그 금액에 실제로 수수료를 뗐다.

    손익(gross_pnl/net_pnl)은 여전히 0 이다 — 이중계산 방지 성질은 그대로다."""
    assert ledger.append_reconcile_backfill_buy(
        symbol=SYMBOL, quantity=BUY16_QTY, avg_price=BUY16_PRICE,
        reconciled_at="2026-09-17T14:20:41+09:00", mode="real",
    ) is True

    row = ledger.load_execution_ledger()[0]
    assert row["source"] == "RECONCILE_BACKFILL"
    assert float(row["fee"]) == KIS_BUY_FEE_16
    assert float(row["gross_pnl"]) == 0.0
    assert float(row["net_pnl"]) == 0.0


def test_reconcile_backfill_sell_uses_realized_cost():
    assert ledger.append_reconcile_backfill_sell(
        symbol=SYMBOL, quantity=SELL16_QTY, exit_price=SELL16_PRICE, entry_price=BUY16_PRICE,
        position_before=SELL16_QTY, position_after=0,
        reconciled_at="2026-09-17T15:00:08+09:00", mode="real",
        exit_reason=config.EXIT_TW_STOP_LOSS,
    ) is True

    row = ledger.load_execution_ledger()[0]
    assert float(row["fee"]) == KIS_SELL_FEE_16
    assert float(row["net_pnl"]) == KIS_REALIZED_PNL_16
    assert float(row["slippage"]) == 0.0


def test_no_ledger_write_path_touches_the_strategy_cost_model(monkeypatch):
    """구조적 가드 — 원장에 레그를 적는 어떤 경로도 전략 판정용 비용모델을
    부르지 않는다. 새 경로가 추가되면서 다시 `compute_trade_cost` 로 계산하면
    여기서 곧바로 터진다."""
    def _forbidden(*args, **kwargs):
        raise AssertionError("원장 실현비용에 전략 판정용 비용모델을 쓰면 안 된다")

    monkeypatch.setattr(TradeCostEngine, "compute_trade_cost", _forbidden)
    monkeypatch.setattr(TradeCostEngine, "compute_net_pnl", _forbidden)

    _record_the_whole_20260917_day()
    ledger.append_broker_direct_execution(
        _FakeDirectOrderResult(order_id="guard-direct", side="SELL", quantity=BUY16_QTY,
                               price=0.0, raw={"ODNO": "guard-direct"}),
        broker=_FakeQuoteBroker(SELL16_PRICE))
    ledger.append_broker_direct_fill(
        {"order_id": "guard-fill", "symbol": SYMBOL, "side": "BUY", "quantity": BUY16_QTY,
         "price": BUY16_PRICE, "timestamp": "20260917142041"}, mode="real")
    ledger.append_reconcile_backfill_buy(
        symbol=SYMBOL, quantity=BUY16_QTY, avg_price=BUY16_PRICE,
        reconciled_at="2026-09-17T14:20:41+09:00", mode="real")
    ledger.append_reconcile_backfill_sell(
        symbol=SYMBOL, quantity=SELL16_QTY, exit_price=SELL16_PRICE, entry_price=BUY16_PRICE,
        position_before=SELL16_QTY, position_after=0,
        reconciled_at="2026-09-17T15:00:08+09:00", mode="real",
        exit_reason=config.EXIT_TW_STOP_LOSS)

    # 전략 모델을 한 번도 부르지 않고 모든 경로가 실제 수수료를 적었다.
    rows = ledger.load_execution_ledger()
    assert len(rows) >= 6
    assert all(str(r.get("fee") or "") != "" for r in rows)


# ── 6. 과거 행은 재작성하지 않는다 ────────────────────────────────────────

def test_rows_written_before_the_cutoff_are_left_exactly_as_they_were():
    """원장은 감사 추적이다 — 그때 기록된 사실을 사후에 고쳐 쓰지 않는다."""
    legacy = {
        "order_id": "legacy-15", "signal_id": "20260917_old", "timestamp": "2026-09-17T14:20:41+09:00",
        "mode": "real", "symbol": SYMBOL, "side": "BUY",
        "requested_qty": BUY16_QTY, "executed_qty": BUY16_QTY,
        "requested_price": BUY16_PRICE, "executed_price": BUY16_PRICE,
        "position_before": 0, "position_after": BUY16_QTY,
        "gross_pnl": 0.0, "fee": LEGACY_BUY_FEE_15, "slippage": 0.0, "net_pnl": 0.0,
        "exit_reason": "", "broker_response": "{}",
    }
    assert ledger.append_execution(legacy) is True

    # 새 레그를 뒤에 기록해도 옛 행은 그대로 15원이어야 한다.
    _record("0016650200", "SELL", SELL16_QTY, SELL16_PRICE, entry_price=BUY16_PRICE,
            position_before=SELL16_QTY, position_after=0, at="2026-09-17T15:00:08+09:00")

    old = [r for r in ledger.load_execution_ledger() if r["order_id"] == "legacy-15"][0]
    assert float(old["fee"]) == LEGACY_BUY_FEE_15


def test_ui_marks_pre_cutoff_rows_as_old_rate_estimates():
    """재작성하지 않는 대신 UI 가 구분한다."""
    import importlib

    page = importlib.import_module("app.ui.pages.11_MACD_자동매매2")

    before = [{"timestamp": "2026-09-17T14:20:41+09:00"}]
    after = [{"timestamp": "2026-09-18T09:30:00+09:00"}]

    assert page._uses_legacy_fee_rate(before) is True
    assert page._uses_legacy_fee_rate(after) is False

    old_row = [{"timestamp": "2026-09-17T14:20:41+09:00", "fee": "15"}]
    new_row = [{"timestamp": "2026-09-18T09:30:00+09:00", "fee": "4"}]
    assert page._fee_display(15.0, old_row).endswith(page.LEGACY_FEE_MARK)
    assert not page._fee_display(4.0, new_row).endswith(page.LEGACY_FEE_MARK)


def test_a_zero_won_fee_is_shown_as_zero_not_as_missing():
    """KIS 도 소액 레그에 0원을 찍는다(2026-09-17 의 1주 6,220원 매수) — 0원과
    "기록 없음"을 같은 '-' 로 뭉개면 KIS 화면과 대조가 안 된다."""
    import importlib

    page = importlib.import_module("app.ui.pages.11_MACD_자동매매2")

    recorded_zero = [{"timestamp": "2026-09-18T09:30:00+09:00", "fee": "0.0"}]
    never_recorded = [{"timestamp": "2026-09-18T09:30:00+09:00", "fee": ""}]

    assert page._fee_display(0.0, recorded_zero) == page._money_display(0.0)
    assert page._fee_display(0.0, never_recorded) == "-"


# ── 7. 전략 판정용 비용모델 불변 ──────────────────────────────────────────
#
# 아래 값들은 2026-09-18 수정 **직전**에 실제로 측정해 박아둔 것이다. 하나라도
# 변하면 손절/익절 판정선이 움직였다는 뜻이고, 그러면 백테스트(H50 / X2-lite+W1a)
# 결과도 더 이상 같지 않다. 요율 변경은 2단계 별도 브랜치 소관이다.

def test_strategy_fee_rates_are_untouched():
    e = TradeCostEngine()
    for sym in ("000660", "0193T0", "0197X0"):
        assert e.fee_rate(sym, "BUY") == pytest.approx(0.00015)
        assert e.fee_rate(sym, "SELL") == pytest.approx(0.00015)
    assert e.realized_fee_rate() == pytest.approx(0.000036396)


def test_strategy_net_pnl_model_is_bit_for_bit_unchanged():
    e = TradeCostEngine()
    r = e.compute_net_pnl("0197X0", 10_000, 9_860, 530,
                          buy_order_type="market", sell_order_type="market")
    assert r["buy_fee"] == pytest.approx(795.0)
    assert r["sell_fee"] == pytest.approx(783.87)
    assert r["slippage"] == pytest.approx(3_157.74)
    assert r["total_cost"] == pytest.approx(4_736.61)
    assert r["net_pnl"] == pytest.approx(-78_936.61)

    hynix = e.compute_net_pnl("000660", 10_000, 9_860, 530,
                              buy_order_type="market", sell_order_type="market")
    assert hynix["transaction_tax"] == pytest.approx(9_406.44)
    assert hynix["net_pnl"] == pytest.approx(-88_343.05)


def test_strategy_trade_cost_and_round_trip_are_unchanged():
    e = TradeCostEngine()
    tc = e.compute_trade_cost("0197X0", "SELL", 10_000, 530, order_type="market")
    assert tc["fee"] == pytest.approx(795.0)
    assert tc["total_cost"] == pytest.approx(795.0)

    assert e.compute_round_trip_cost_pct("0197X0", "market") == pytest.approx(0.09)
    assert e.compute_round_trip_cost_pct("0197X0", "limit") == pytest.approx(0.05)
    assert e.compute_round_trip_cost_pct("000660", "market") == pytest.approx(0.27)
    assert e.compute_round_trip_cost_pct("000660", "limit") == pytest.approx(0.23)


@pytest.mark.parametrize("symbol,entry,exit_price,qty,expected", [
    ("0197X0", 10_000, 9_860, 530, -1.48937),
    ("0197X0", 6_940, 6_950, 1_114, 0.05402746806430083),
    ("0197X0", 15_010, 15_300, 662, 1.8411758726810525),
    ("000660", 10_000, 9_860, 530, -1.66685),
    ("000660", 6_940, 6_950, 1_114, -0.12623189792993547),
    ("000660", 15_010, 15_300, 662, 1.6576981911354163),
])
def test_net_return_pct_drives_tp_sl_and_must_not_move(symbol, entry, exit_price, qty, expected):
    """TP/SL 판정선 그 자체 — 이 값이 변하면 실거래 청산 시점이 달라진다."""
    from app.trading.macd2.worker import _net_return_pct

    assert _net_return_pct(symbol, entry, exit_price, qty) == pytest.approx(expected, abs=1e-9)


def test_order_sizing_margin_still_uses_the_conservative_strategy_rate():
    """주문 수량 산정의 안전여유는 **주문 전** 판단이라 실현비용 경로가 아니다.
    보수적인(큰) 요율을 그대로 두는 편이 현금 부족으로 주문이 튕기는 것을
    막는다 — 2026-09-18 수정 범위 밖이라는 사실을 못박아 둔다."""
    from app.utils.stock_utils import get_tick_size

    price = 6_250.0
    margin = order_executor.compute_order_safety_margin_pct(price, SYMBOL)
    expected = TradeCostEngine().fee_rate(SYMBOL, "BUY") * 100.0 + get_tick_size(price) / price * 100.0
    assert margin == pytest.approx(expected)
