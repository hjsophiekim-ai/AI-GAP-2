"""실거래 원장/UI ↔ KIS 계좌 실현손익 1원 단위 parity — 2026-09-17.

실제 사고
---------
2026-09-17 인버스(0197X0) 실거래에서 우리 원장이 **실현손익 1,756원 / 수수료
15원**을 표시했는데 KIS 계좌는 **1,717원 / 8원**이었다. 원인 셋 모두 "체결이
끝난 거래의 손익을 추정값으로 계산"한 데서 나왔다.

  1. 수수료율이 추정치(0.015%) — 실제 0.0036396% 의 약 4.1배
  2. 체결 후 손익에서 **예상 슬리피지 59.45원을 또 차감**
  3. 체결금액을 `평균단가 x 수량` 으로 계산 — 부분체결이 섞인 16주 매수의
     실제 체결금액은 98,155원인데 6,134 x 16 = 98,144 로 11원 어긋났다

이 파일은 그날의 **실제 KIS 체결 데이터**를 fixture 로 고정한다. 수치는
`inquire-daily-ccld` 원본(output1/output2)에서 그대로 가져왔다.
"""
from __future__ import annotations

import pytest

from app.trading.kis_realized import realized_summary_from_fills, reconcile_with_kis
from app.trading.trading_cost_engine import TradeCostEngine

SYMBOL = "0197X0"

#: 2026-09-17 KIS inquire-daily-ccld output1 실측 (amount = tot_ccld_amt)
FILLS_20260917 = [
    {"symbol": SYMBOL, "side": "BUY", "order_id": "0007472700", "quantity": 1,
     "amount": 6220.0, "avg_price": 6220.0, "timestamp": "20260917095504"},
    {"symbol": SYMBOL, "side": "SELL", "order_id": "0015360300", "quantity": 1,
     "amount": 6100.0, "avg_price": 6100.0, "timestamp": "20260917141245"},
    {"symbol": SYMBOL, "side": "BUY", "order_id": "0015566900", "quantity": 16,
     "amount": 98155.0, "avg_price": 6134.0, "timestamp": "20260917142041"},
    {"symbol": SYMBOL, "side": "SELL", "order_id": "0016650200", "quantity": 16,
     "amount": 100000.0, "avg_price": 6250.0, "timestamp": "20260917150008"},
]

#: 같은 조회의 output2 집계
TOTALS_20260917 = {"tot_ord_qty": "34", "tot_ccld_qty": "34",
                   "tot_ccld_amt": "210475", "prsm_tlex_smtl": "8",
                   "pchs_avg_pric": "6190.4412"}

#: KIS 계좌 화면이 보여준 값 — 여기에 1원 단위로 맞춰야 한다
KIS_BUY_AMOUNT = 104375
KIS_SELL_AMOUNT = 106100
KIS_FEE = 8
KIS_TAX = 0
KIS_REALIZED_PNL = 1717


def test_realized_summary_matches_kis_to_the_won():
    s = realized_summary_from_fills(FILLS_20260917, symbol=SYMBOL).as_dict()
    assert s["buy_qty"] == 17
    assert s["sell_qty"] == 17
    assert s["buy_amount"] == KIS_BUY_AMOUNT
    assert s["sell_amount"] == KIS_SELL_AMOUNT
    assert s["fee"] == KIS_FEE
    assert s["tax"] == KIS_TAX
    assert s["realized_pnl"] == KIS_REALIZED_PNL
    assert s["open_qty"] == 0, "그날 포지션은 전량 정리됐다"


def test_reconcile_against_kis_aggregate_has_zero_drift():
    r = reconcile_with_kis(FILLS_20260917, TOTALS_20260917, symbol=SYMBOL)
    assert r["ok"] is True
    assert r["amount_diff"] == 0.0, "체결금액 합이 KIS tot_ccld_amt 와 달라졌다"
    assert r["fee_diff"] == 0.0, "우리 수수료 추정이 KIS prsm_tlex_smtl 와 달라졌다"
    assert r["realized_pnl"] == KIS_REALIZED_PNL


def test_kis_reported_fee_always_wins_over_our_estimate():
    """KIS 가 실제 제비용을 알려주면 추정값을 쓰지 않는다."""
    s = realized_summary_from_fills(
        FILLS_20260917, symbol=SYMBOL, fee_total_override=99.0).as_dict()
    assert s["fee"] == 99.0
    assert s["realized_pnl"] == 1725 - 99


def test_actual_fill_amount_is_used_not_price_times_quantity():
    """부분체결이 섞인 16주 매수 — 평균단가 x 수량으로 계산하면 11원 틀린다."""
    buy = [f for f in FILLS_20260917 if f["order_id"] == "0015566900"][0]
    assert buy["amount"] == 98155.0
    assert buy["avg_price"] * buy["quantity"] == 98144.0
    s = realized_summary_from_fills(FILLS_20260917, symbol=SYMBOL)
    assert s.buy_amount == KIS_BUY_AMOUNT   # 98,155 + 6,220
    # 단가 기반이었다면 104,364 가 되어 KIS 와 11원 어긋난다
    assert s.buy_amount != 6220 + 98144


def test_no_slippage_is_subtracted_from_a_realized_trade():
    """체결이 끝난 거래에 예상 슬리피지를 다시 빼면 안 된다(2026-09-17 원인 ②)."""
    e = TradeCostEngine()
    r = e.compute_realized_pnl(SYMBOL, buy_amount=98155, sell_amount=100000)
    assert r["slippage"] == 0.0
    assert r["net_pnl"] == 100000 - 98155 - r["total_cost"]


def test_realized_rate_is_separate_from_the_strategy_cost_model():
    """전략 판정용 요율은 **건드리지 않았다** — 손절/익절 판정선과 백테스트가
    거기에 묶여 있어 재검증 없이 바꿀 수 없다(2026-09-17 사용자 결정, 2단계)."""
    e = TradeCostEngine()
    assert e.fee_rate(SYMBOL, "BUY") == pytest.approx(0.00015)
    assert e.fee_rate(SYMBOL, "SELL") == pytest.approx(0.00015)
    assert e.realized_fee_rate() == pytest.approx(0.000036396)


def test_strategy_net_return_pct_is_unchanged_by_this_fix():
    """`_net_return_pct` 는 TP/SL 판정에 그대로 쓰이므로 값이 변하면 안 된다."""
    from app.trading.macd2.worker import _net_return_pct

    assert _net_return_pct(SYMBOL, 10000, 9860, 530) == pytest.approx(-1.4894, abs=1e-4)


def test_etf_has_no_transaction_tax():
    """그날 KIS 제세금도 0원이었다 — ETF 증권거래세 면제."""
    e = TradeCostEngine()
    r = e.compute_realized_pnl(SYMBOL, buy_amount=98155, sell_amount=100000)
    assert r["transaction_tax"] == 0.0


def test_partial_position_leaves_the_open_lot_unrealized():
    """전량 청산되지 않으면 남은 수량은 실현손익에 들어가지 않는다(FIFO)."""
    fills = [
        {"symbol": SYMBOL, "side": "BUY", "order_id": "b1", "quantity": 10,
         "amount": 60000.0, "timestamp": "20260917100000"},
        {"symbol": SYMBOL, "side": "SELL", "order_id": "s1", "quantity": 4,
         "amount": 25000.0, "timestamp": "20260917110000"},
    ]
    s = realized_summary_from_fills(fills, symbol=SYMBOL)
    assert s.open_qty == 6
    assert s.round_trips[0].quantity == 4
    assert s.gross_pnl == pytest.approx(25000 - 24000)   # 4주 매수원가 24,000


# ── KIS 계좌 화면(TTTC8715R) 필드와 1원 단위로 같은가 ──────────────────────
#
# 2026-09-17 실조회 output2 원본. 사용자가 계좌 화면에서 본 "매수금액 104,379"의
# 정체가 여기서 확정된다 — 체결금액(104,375)이 아니라 **정산금액**이다.
TTTC8715R_TOTALS_20260917 = {
    "sll_qty_smtl": "17", "sll_tr_amt_smtl": "106100", "sll_fee_smtl": "4",
    "sll_tltx_smtl": "0", "sll_excc_amt_smtl": "106096",
    "buyqty_smtl": "17", "buy_tr_amt_smtl": "104375", "buy_fee_smtl": "4",
    "buy_tax_smtl": "0", "buy_excc_amt_smtl": "104379",
    "tot_qty": "34", "tot_tr_amt": "210475", "tot_fee": "8", "tot_tltx": "0",
    "tot_excc_amt": "210475", "tot_rlzt_pfls": "1717", "tot_pftrt": "1.64502994",
}


def test_account_screen_fields_match_to_the_won():
    from app.trading.kis_realized import account_realized_from_kis

    a = account_realized_from_kis(TTTC8715R_TOTALS_20260917)
    assert a["buy_qty"] == 17 and a["sell_qty"] == 17
    assert a["buy_trade_amount"] == 104375       # 체결금액
    assert a["buy_settlement_amount"] == 104379  # 계좌 화면의 "매수금액"
    assert a["sell_trade_amount"] == 106100
    assert a["sell_settlement_amount"] == 106096
    assert a["buy_fee"] == 4 and a["sell_fee"] == 4
    assert a["fee"] == 8
    assert a["tax"] == 0
    assert a["realized_pnl"] == 1717
    assert a["return_pct"] == pytest.approx(1.64502994)


def test_the_four_won_gap_is_exactly_the_buy_side_fee():
    """사용자가 본 104,379 와 체결금액 104,375 의 4원 차이 = 매수수수료."""
    from app.trading.kis_realized import account_realized_from_kis

    a = account_realized_from_kis(TTTC8715R_TOTALS_20260917)
    assert a["buy_settlement_amount"] - a["buy_trade_amount"] == a["buy_fee"] == 4
    assert a["sell_trade_amount"] - a["sell_settlement_amount"] == a["sell_fee"] == 4


def test_realized_pnl_agrees_whichever_amount_basis_is_used():
    """체결금액 기준과 정산금액 기준이 같은 실현손익을 내야 한다."""
    from app.trading.kis_realized import verify_kis_internal_consistency

    c = verify_kis_internal_consistency(TTTC8715R_TOTALS_20260917)
    assert c["ok"] is True
    assert c["by_trade_amount"] == 1717      # 106,100 - 104,375 - 8
    assert c["by_settlement_amount"] == 1717  # 106,096 - 104,379
    assert c["kis_reported"] == 1717


def test_kis_screen_and_fill_reconstruction_agree():
    """계좌 화면(TTTC8715R)과 체결내역 재구성(fallback)이 같은 답을 내야 한다."""
    from app.trading.kis_realized import account_realized_from_kis

    screen = account_realized_from_kis(TTTC8715R_TOTALS_20260917)
    rebuilt = realized_summary_from_fills(
        FILLS_20260917, symbol=SYMBOL, fee_total_override=8).as_dict()
    assert screen["buy_trade_amount"] == rebuilt["buy_amount"]
    assert screen["sell_trade_amount"] == rebuilt["sell_amount"]
    assert screen["fee"] == rebuilt["fee"]
    assert screen["realized_pnl"] == rebuilt["realized_pnl"] == KIS_REALIZED_PNL
