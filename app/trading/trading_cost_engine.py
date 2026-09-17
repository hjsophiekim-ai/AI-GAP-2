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

# ── 두 가지 요율이 **일부러** 따로 있다 (2026-09-17) ────────────────────────
#
#   전략 판정용 (아래 *_fee_rate)      : 0.00015  — 기존값 유지
#   실현손익용 (realized_fee_rate)      : 0.000036396 — KIS 실측값
#
# 왜 하나로 합치지 않았나: 전략 요율은 `worker._net_return_pct` 를 거쳐 손절/
# 익절 판정선과 모든 백테스트에 들어가 있다. 이 값을 바꾸면 실거래 동작이
# 즉시 달라지므로, 재백테스트 없이 건드릴 수 없다(2026-09-17 사용자 결정으로
# **2단계 별도 브랜치**에서 검증 후 적용하기로 했다).
#
# 반면 **이미 체결된 거래의 실현손익**은 추정이 아니라 사실이므로 지금 바로
# KIS 와 맞춰야 한다. 그 경로만 realized_fee_rate 를 쓴다.
#
# realized_fee_rate 근거 (KIS inquire-daily-ccld, 2026-09-17 실계좌):
#   총 체결금액 tot_ccld_amt = 210,475원 / 총 제비용 prsm_tlex_smtl = 8원
#   -> 0.0036396% (KIS 온라인 수수료, 유관기관 제비용 포함)
#   레그별 검산: 6,220→0 / 6,100→0 / 98,155→4 / 100,000→4  합 8원 ✅
_KIS_REALIZED_FEE_RATE = 0.000036396

_DEFAULT_COST_CONFIG = {
    "domestic_buy_fee_rate": 0.00015,
    "domestic_sell_fee_rate": 0.00015,
    "etf_buy_fee_rate": 0.00015,
    "etf_sell_fee_rate": 0.00015,
    "realized_fee_rate": _KIS_REALIZED_FEE_RATE,
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

    def fee_rate(self, symbol: str, side: str) -> float:
        """Public accessor for the configured fee rate (config.yaml trading_cost) —
        e.g. for order-sizing code that needs the real fee rate without duplicating
        the ETF/domestic branching here."""
        return self._fee_rate(symbol, side)

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

    def realized_fee_rate(self) -> float:
        """실현손익 전용 수수료율 — 전략 판정용 요율과 **의도적으로 분리**돼 있다
        (모듈 상단 주석 참조). 전략 요율을 바꾸면 손절선이 움직이지만 이 값은
        이미 끝난 거래의 기록에만 쓰이므로 안전하다."""
        return float(self._cfg.get("realized_fee_rate", _KIS_REALIZED_FEE_RATE))

    def realized_leg_fee(self, amount: float, *, fee_override: float | None = None) -> float:
        """체결이 끝난 **레그 1건**의 매매비용 — 거래원장 `fee` 컬럼의 단일 출처.

        우선순위는 매수/매도가 완전히 같다(2026-09-18):

            1. KIS 가 그 레그의 실제 제비용을 알려주면 **그 값**   (fee_override)
            2. 없으면 realized_fee_rate 로 환산 + 원 단위 반올림

        전략 판정용 요율(`_fee_rate` / `compute_trade_cost`)은 **여기에 절대
        들어오지 않는다**. 2026-09-17 원장이 매수 수수료를 15원으로 적은 것이
        정확히 그 경로였다 — 98,155 x 0.00015 = 14.72 -> 15원, KIS 실제는 4원.
        요율을 쓰더라도 KIS 와 같은 방식으로 레그별 원 단위 반올림한다.
        """
        if fee_override is not None:
            return float(round(float(fee_override)))
        return float(round(float(amount) * self.realized_fee_rate()))

    def compute_realized_pnl(
        self, symbol: str, buy_amount: float, sell_amount: float,
        buy_fee_override: float | None = None,
        sell_fee_override: float | None = None,
    ) -> dict:
        """**실제 체결된** 매수/매도 금액으로 실현손익을 계산한다 (KIS 계좌 기준).

        `compute_net_pnl` 과 두 가지가 다르고, 그 두 가지가 정확히 2026-09-17
        실거래 불일치의 원인이었다.

        1) **슬리피지를 빼지 않는다.** 슬리피지는 "주문가 대비 체결가가 얼마나
           밀릴까"를 *예측*하는 값이다. 이미 체결된 가격으로 계산하는 실현손익에
           다시 빼면 이중 차감이다. (그날 59.45원이 이렇게 사라졌다.)
        2) **단가 x 수량이 아니라 체결금액을 받는다.** KIS 의 체결금액
           (`tot_ccld_amt`)은 부분체결이 섞이면 단가 x 수량과 다르다 — 그날
           16주 매수가 98,155원이었는데 평균단가 6,134 x 16 = 98,144 로 11원
           어긋났다.

        수수료는 레그별로 `realized_leg_fee` 를 거친다 — KIS 가 실제 제비용을
        알려주면 그 값이, 아니면 realized_fee_rate 환산값이 쓰인다(원 단위
        반올림, KIS 표기와 같은 방식). 매수/매도 override 를 따로 받는 이유는
        한쪽만 KIS 값이 있는 경우가 있기 때문이다(예: 매도 레그를 기록하는
        시점에 매수 레그의 제비용은 이미 원장에 남아 있다).
        ETF 증권거래세는 0 이다(면제) — 그날 KIS 제세금도 0원이었다.
        """
        buy_amount = float(buy_amount)
        sell_amount = float(sell_amount)
        buy_fee = self.realized_leg_fee(buy_amount, fee_override=buy_fee_override)
        sell_fee = self.realized_leg_fee(sell_amount, fee_override=sell_fee_override)
        tax = round(sell_amount * self._tax_rate(symbol, "SELL"))
        clearing = round((buy_amount + sell_amount) * self._cfg.get("clearing_fee_rate", 0.0))
        gross = sell_amount - buy_amount
        total_cost = buy_fee + sell_fee + tax + clearing
        return {
            "buy_amount": round(buy_amount, 2), "sell_amount": round(sell_amount, 2),
            "gross_pnl": round(gross, 2), "buy_fee": float(buy_fee), "sell_fee": float(sell_fee),
            "transaction_tax": float(tax), "clearing_fee": float(clearing),
            "slippage": 0.0, "total_cost": float(total_cost),
            "net_pnl": round(gross - total_cost, 2),
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
