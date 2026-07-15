"""trading_cost_engine.py — 한국투자증권(KIS) 실거래 기준 거래비용(수수료/거래세/
슬리피지) 계산 엔진.

기존에는 실현/미실현손익을 매수가-매도가 차이(GrossPnL)만으로 계산했다. 실제
계좌 기준으로는 매수수수료/매도수수료/증권거래세(ETF는 면제)/슬리피지가 추가로
차감되어야 하므로, 이 엔진을 거쳐 NetPnL을 산출한다(docs/requirements.md 섹션 2).

수수료율/세율/슬리피지율은 코드에 하드코딩하지 않고 config.yaml의 trading_cost
섹션에서 읽는다(app.config.get_config().trading_cost) — 실제 KIS 고시 요율로
운영 전 반드시 재확인해야 한다.
"""

from __future__ import annotations

from typing import Optional

# ETF/ETN으로 취급할 종목코드 — 이 프로젝트에서는 0197X0(SOL SK하이닉스선물단일종목
# 인버스2X)이 유일하다. 종목이 늘어나면 이 set만 확장하면 된다(정식 종목마스터
# 연동 전까지의 근사).
ETF_ETN_SYMBOLS = frozenset({"0193T0", "0197X0"})

_DEFAULT_COST_CONFIG = {
    "domestic_buy_fee_rate": 0.00015,
    "domestic_sell_fee_rate": 0.00015,
    "etf_buy_fee_rate": 0.00015,
    "etf_sell_fee_rate": 0.00015,
    "transaction_tax_rate": 0.0018,
    "etf_transaction_tax_rate": 0.0,
    "clearing_fee_rate": 0.0,
    "slippage_rate_default": 0.0002,
    "slippage_rate_market_order": 0.0003,
    "slippage_rate_limit_order": 0.0001,
    "min_commission_krw": 0.0,
}


def is_etf_or_etn(symbol: str) -> bool:
    return symbol in ETF_ETN_SYMBOLS


class TradeCostEngine:
    """종목코드 + 매매방향(BUY/SELL) + 체결가 + 수량 + 주문유형(market/limit)을
    받아 수수료/거래세/청산수수료/슬리피지를 계산해 GrossPnL → NetPnL 변환에
    필요한 모든 값을 반환한다."""

    def __init__(self, cost_config: Optional[dict] = None):
        if cost_config is not None:
            merged = dict(_DEFAULT_COST_CONFIG)
            merged.update(cost_config)
            self._cfg = merged
        else:
            try:
                from app.config import get_config

                merged = dict(_DEFAULT_COST_CONFIG)
                merged.update(get_config().trading_cost)
                self._cfg = merged
            except Exception:
                self._cfg = dict(_DEFAULT_COST_CONFIG)

    def _fee_rate(self, symbol: str, side: str) -> float:
        etf = is_etf_or_etn(symbol)
        if side == "BUY":
            return self._cfg["etf_buy_fee_rate"] if etf else self._cfg["domestic_buy_fee_rate"]
        return self._cfg["etf_sell_fee_rate"] if etf else self._cfg["domestic_sell_fee_rate"]

    def _tax_rate(self, symbol: str, side: str) -> float:
        if side != "SELL":
            return 0.0  # 거래세는 매도 시에만 부과된다.
        if is_etf_or_etn(symbol):
            return self._cfg.get("etf_transaction_tax_rate", 0.0)
        return self._cfg.get("transaction_tax_rate", 0.0)

    def _slippage_rate(self, order_type: str) -> float:
        if order_type == "market":
            return self._cfg.get("slippage_rate_market_order", self._cfg["slippage_rate_default"])
        if order_type == "limit":
            return self._cfg.get("slippage_rate_limit_order", self._cfg["slippage_rate_default"])
        return self._cfg["slippage_rate_default"]

    def estimate_slippage_adjusted_price(self, symbol: str, side: str, price: float, order_type: str = "limit") -> float:
        """예상 체결가(주문가 대비 슬리피지 반영). 매수는 더 비싸게, 매도는 더
        싸게 — 항상 트레이더에게 불리한 방향으로 보수적으로 조정한다."""
        rate = self._slippage_rate(order_type)
        if side == "BUY":
            return round(price * (1 + rate), 4)
        return round(price * (1 - rate), 4)

    def compute_trade_cost(self, symbol: str, side: str, executed_price: float, quantity: int, order_type: str = "limit") -> dict:
        """1건의 체결(매수 또는 매도)에 대한 수수료/거래세/청산수수료를 계산한다."""
        notional = executed_price * quantity
        fee = notional * self._fee_rate(symbol, side)
        min_fee = self._cfg.get("min_commission_krw", 0.0)
        if min_fee and fee < min_fee:
            fee = min_fee
        tax = notional * self._tax_rate(symbol, side)
        clearing = notional * self._cfg.get("clearing_fee_rate", 0.0)
        return {
            "notional": round(notional, 2), "fee": round(fee, 2), "tax": round(tax, 2),
            "clearing_fee": round(clearing, 2), "total_cost": round(fee + tax + clearing, 2),
        }

    def compute_round_trip_cost_pct(self, symbol: str, order_type: str = "limit") -> float:
        """왕복(매수+매도) 거래비용을 대략적인 %로 근사한다(진입 게이트/기대값 계산용)."""
        buy_fee = self._fee_rate(symbol, "BUY")
        sell_fee = self._fee_rate(symbol, "SELL")
        sell_tax = self._tax_rate(symbol, "SELL")
        clearing = self._cfg.get("clearing_fee_rate", 0.0) * 2
        slippage = self._slippage_rate(order_type) * 2
        return round((buy_fee + sell_fee + sell_tax + clearing + slippage) * 100.0, 4)

    def compute_net_pnl(
        self, symbol: str, entry_price: float, exit_price: float, quantity: int,
        buy_order_type: str = "limit", sell_order_type: str = "limit",
    ) -> dict:
        """GrossPnL → NetPnL 변환(명세 2.5)."""
        gross_pnl = (exit_price - entry_price) * quantity
        buy_cost = self.compute_trade_cost(symbol, "BUY", entry_price, quantity, buy_order_type)
        sell_cost = self.compute_trade_cost(symbol, "SELL", exit_price, quantity, sell_order_type)
        total_tax = buy_cost["tax"] + sell_cost["tax"]
        total_clearing = buy_cost["clearing_fee"] + sell_cost["clearing_fee"]
        slippage_cost = (
            self._slippage_rate(buy_order_type) * entry_price * quantity
            + self._slippage_rate(sell_order_type) * exit_price * quantity
        )
        net_pnl = gross_pnl - buy_cost["fee"] - sell_cost["fee"] - total_tax - total_clearing - slippage_cost
        return {
            "gross_pnl": round(gross_pnl, 2), "buy_fee": round(buy_cost["fee"], 2),
            "sell_fee": round(sell_cost["fee"], 2), "transaction_tax": round(total_tax, 2),
            "clearing_fee": round(total_clearing, 2), "slippage": round(slippage_cost, 2),
            "total_cost": round(buy_cost["fee"] + sell_cost["fee"] + total_tax + total_clearing + slippage_cost, 2),
            "net_pnl": round(net_pnl, 2),
        }

    def compute_unrealized_net_pnl(self, symbol: str, entry_price: float, current_price: float, quantity: int, order_type: str = "limit") -> dict:
        """미실현손익도 수수료 차감 후 표시(명세 2.10) — "지금 판다면"을 가정해
        매도수수료/거래세/슬리피지를 선차감한다(매수수수료는 진입 시점에 이미 발생)."""
        gross_unrealized = (current_price - entry_price) * quantity
        buy_cost = self.compute_trade_cost(symbol, "BUY", entry_price, quantity, order_type)
        sell_cost = self.compute_trade_cost(symbol, "SELL", current_price, quantity, order_type)
        slippage_cost = self._slippage_rate(order_type) * current_price * quantity
        net_unrealized = (
            gross_unrealized - buy_cost["fee"] - sell_cost["fee"] - sell_cost["tax"]
            - sell_cost["clearing_fee"] - slippage_cost
        )
        return {
            "gross_unrealized_pnl": round(gross_unrealized, 2),
            "already_paid_buy_fee": round(buy_cost["fee"], 2),
            "estimated_exit_fee": round(sell_cost["fee"], 2), "estimated_exit_tax": round(sell_cost["tax"], 2),
            "estimated_slippage": round(slippage_cost, 2),
            "net_unrealized_pnl": round(net_unrealized, 2),
        }
