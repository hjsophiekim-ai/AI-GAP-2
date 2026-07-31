"""TSLA_AUTO overseas trading cost engine — TSLL/TSLZ USD 수수료/세금/슬리피지.

app/trading/trading_cost_engine.py(국내 전용)와 동일한 패턴(config.yaml에서
요율을 읽고, 하드코딩하지 않음)을 재사용하되 완전히 별도 클래스로 분리한다
(docs/TSLA_AUTO_COPY_MAP.md — REWRITE_FOR_KIS_OVERSEAS). 국내
``TradeCostEngine``은 이 모듈에서 import하지 않는다.

**중요 — 확인되지 않은 요율**: KIS가 실제 부과하는 미국주식 매매수수료율·
최소수수료·환전수수료율은 이 세션에서 공식 문서로 확인하지 못했다
(docs/TSLA_AUTO_LOGIC.md §비용·손익, §KIS_OVERSEAS_API_CONFIRMATION_REQUIRED).
기본값은 전부 ``0.0``(비용 없음으로 과소평가하지 않도록, 대신 명시적으로
"미설정" 상태를 유지)이며, 실사용 전 ``config.yaml``의 ``overseas_trading_cost``
섹션에 실제 KIS 고시 요율을 반드시 채워야 한다. SEC Section 31 fee/FINRA TAF는
미국 규제기관이 공개 고시하는 요율(자주 변경됨 — 최신값 재확인 필요)로,
2026년 초 기준 공개된 근사치를 기본값으로 두되 설정으로 언제든 덮어쓸 수 있다.
"""
from __future__ import annotations

from typing import Optional

_DEFAULT_OVERSEAS_COST_CONFIG = {
    # KIS 해외주식 매매수수료 — 미확인(§KIS_OVERSEAS_API_CONFIRMATION_REQUIRED).
    # 확인 전까지 0.0 유지 — 비용을 과소평가하고 있다는 사실 자체를 숨기지 않는다.
    "overseas_buy_fee_rate": 0.0,
    "overseas_sell_fee_rate": 0.0,
    "min_commission_usd": 0.0,
    # SEC Section 31 fee — 매도 시에만, 명목금액 기준(공개 고시 요율, 수시 변경).
    "sec_section31_fee_rate": 0.000008,
    # FINRA TAF — 매도 시에만, 주당 요율 + 거래당 상한(공개 고시 요율, 수시 변경).
    "finra_taf_rate_per_share": 0.000166,
    "finra_taf_cap_usd": 8.30,
    # 환전수수료·스프레드 — 미확인, 기본 0.0(자동환전 기본 OFF와 일관).
    "fx_conversion_fee_rate": 0.0,
    "fx_spread_rate": 0.0,
    "slippage_rate_default": 0.0002,
    "slippage_rate_limit_order": 0.0001,
}


class OverseasTradeCostEngine:
    """종목코드 + 매매방향(BUY/SELL) + 체결가(USD) + 수량 + 주문유형을 받아
    수수료/SEC/FINRA/환전비용/슬리피지를 계산해 Gross USD PnL -> Net USD PnL
    변환에 필요한 모든 값을 반환한다. 실제 KIS 체결내역에 비용이 포함되어
    있으면 이 추정값보다 그 실제값을 항상 우선 사용해야 한다(호출부 책임 —
    docs §비용·손익)."""

    def __init__(self, cost_config: Optional[dict] = None):
        if cost_config is not None:
            merged = dict(_DEFAULT_OVERSEAS_COST_CONFIG)
            merged.update(cost_config)
            self._cfg = merged
        else:
            try:
                from app.config import get_config

                merged = dict(_DEFAULT_OVERSEAS_COST_CONFIG)
                merged.update(get_config()._raw.get("tsla_auto", {}).get("overseas_trading_cost", {}))
                self._cfg = merged
            except Exception:
                self._cfg = dict(_DEFAULT_OVERSEAS_COST_CONFIG)

    def fee_rate(self, side: str) -> float:
        return self._cfg["overseas_buy_fee_rate"] if side == "BUY" else self._cfg["overseas_sell_fee_rate"]

    def _regulatory_fees(self, side: str, notional: float, quantity: int) -> tuple[float, float]:
        """(sec_fee, finra_taf) — 매도에만 부과."""
        if side != "SELL":
            return 0.0, 0.0
        sec_fee = notional * float(self._cfg.get("sec_section31_fee_rate", 0.0))
        finra_taf = min(
            quantity * float(self._cfg.get("finra_taf_rate_per_share", 0.0)),
            float(self._cfg.get("finra_taf_cap_usd", 0.0)) or float("inf"),
        )
        return round(sec_fee, 4), round(finra_taf, 4)

    def _slippage_rate(self, order_type: str) -> float:
        if order_type == "limit":
            return self._cfg.get("slippage_rate_limit_order", self._cfg["slippage_rate_default"])
        return self._cfg["slippage_rate_default"]

    def compute_trade_cost_usd(self, side: str, executed_price: float, quantity: int, order_type: str = "limit") -> dict:
        """1건 체결(매수 또는 매도)의 USD 비용 breakdown."""
        notional = executed_price * quantity
        fee = notional * self.fee_rate(side)
        min_fee = self._cfg.get("min_commission_usd", 0.0)
        if min_fee and fee < min_fee:
            fee = min_fee
        sec_fee, finra_taf = self._regulatory_fees(side, notional, quantity)
        fx_cost = notional * (float(self._cfg.get("fx_conversion_fee_rate", 0.0)) + float(self._cfg.get("fx_spread_rate", 0.0)))
        total = fee + sec_fee + finra_taf + fx_cost
        return {
            "notional_usd": round(notional, 4), "fee_usd": round(fee, 4),
            "sec_fee_usd": sec_fee, "finra_taf_usd": finra_taf, "fx_cost_usd": round(fx_cost, 4),
            "total_cost_usd": round(total, 4),
        }

    def compute_net_pnl_usd(
        self, entry_price: float, exit_price: float, quantity: int,
        buy_order_type: str = "limit", sell_order_type: str = "limit",
        *, actual_cost_usd: Optional[float] = None,
    ) -> dict:
        """Gross USD PnL -> Net USD PnL. ``actual_cost_usd``(실제 KIS 체결내역
        비용)가 주어지면 추정 비용 대신 그 값을 항상 우선 사용한다(docs §비용·손익:
        "실제 KIS 체결내역 비용이 제공되면 추정값보다 우선한다")."""
        gross_pnl = (exit_price - entry_price) * quantity
        buy_cost = self.compute_trade_cost_usd("BUY", entry_price, quantity, buy_order_type)
        sell_cost = self.compute_trade_cost_usd("SELL", exit_price, quantity, sell_order_type)
        slippage_cost = (
            self._slippage_rate(buy_order_type) * entry_price * quantity
            + self._slippage_rate(sell_order_type) * exit_price * quantity
        )
        estimated_total_cost = buy_cost["total_cost_usd"] + sell_cost["total_cost_usd"] + slippage_cost
        total_cost = float(actual_cost_usd) if actual_cost_usd is not None else estimated_total_cost
        net_pnl = gross_pnl - total_cost
        return {
            "gross_pnl_usd": round(gross_pnl, 4),
            "buy_cost_usd": buy_cost, "sell_cost_usd": sell_cost,
            "slippage_usd": round(slippage_cost, 4),
            "estimated_total_cost_usd": round(estimated_total_cost, 4),
            "total_cost_usd": round(total_cost, 4),
            "cost_source": "actual_kis" if actual_cost_usd is not None else "estimated",
            "net_pnl_usd": round(net_pnl, 4),
        }
