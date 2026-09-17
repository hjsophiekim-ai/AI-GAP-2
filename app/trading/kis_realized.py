"""KIS 실체결 기반 실현손익 — 원장/UI 의 truth source (2026-09-17).

왜 이 모듈이 따로 있나
----------------------
2026-09-17 실거래에서 우리 원장이 실현손익 1,756원 / 수수료 15원을 표시했는데
KIS 계좌는 1,717원 / 8원이었다. 원인은 셋이었고 전부 "추정값으로 실현손익을
계산한" 데서 나왔다.

  1. 수수료율이 추정치(0.015%)였다 — 실제는 0.0036396% 로 약 4.1배 과다.
  2. **이미 체결된** 거래의 손익에서 "예상 슬리피지"를 또 뺐다(59.45원).
     슬리피지는 주문 전 예측값이지 체결 후 손익 항목이 아니다.
  3. 체결금액을 `평균단가 x 수량` 으로 계산했다. 부분체결이 섞이면 어긋난다 —
     그날 16주 매수가 실제 98,155원인데 6,134 x 16 = 98,144 로 11원 달랐다.

그래서 **체결 이후의 숫자는 추정하지 않는다.** KIS `inquire-daily-ccld` 가
주는 체결금액(`tot_ccld_amt`)과 제비용(`prsm_tlex_smtl`)을 그대로 쓴다.

전략 판정용 비용모델(`TradeCostEngine.compute_net_pnl`, `worker._net_return_pct`,
TP/SL)은 **이 모듈과 무관하게 그대로다** — 그쪽을 바꾸면 손절선이 움직이므로
재백테스트를 거쳐 별도로 결정한다(2026-09-17 사용자 결정).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Iterable, Optional

from app.trading.trading_cost_engine import TradeCostEngine


@dataclass
class RealizedRoundTrip:
    """FIFO 로 맺어진 매수-매도 한 쌍(부분 매칭 포함)."""
    symbol: str
    quantity: int
    buy_amount: float
    sell_amount: float
    buy_order_id: str = ""
    sell_order_id: str = ""

    @property
    def gross_pnl(self) -> float:
        return self.sell_amount - self.buy_amount


@dataclass
class RealizedSummary:
    symbol: str = ""
    buy_qty: int = 0
    sell_qty: int = 0
    buy_amount: float = 0.0
    sell_amount: float = 0.0
    fee: float = 0.0
    tax: float = 0.0
    gross_pnl: float = 0.0
    realized_pnl: float = 0.0
    open_qty: int = 0
    open_amount: float = 0.0
    round_trips: list = field(default_factory=list)

    @property
    def avg_buy_price(self) -> Optional[float]:
        return (self.buy_amount / self.buy_qty) if self.buy_qty else None

    @property
    def avg_sell_price(self) -> Optional[float]:
        return (self.sell_amount / self.sell_qty) if self.sell_qty else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "buy_qty": self.buy_qty, "sell_qty": self.sell_qty,
            "buy_amount": round(self.buy_amount, 2), "sell_amount": round(self.sell_amount, 2),
            "avg_buy_price": (round(self.avg_buy_price, 4) if self.avg_buy_price is not None else None),
            "avg_sell_price": (round(self.avg_sell_price, 4) if self.avg_sell_price is not None else None),
            "fee": round(self.fee, 2), "tax": round(self.tax, 2),
            "gross_pnl": round(self.gross_pnl, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "open_qty": self.open_qty, "open_amount": round(self.open_amount, 2),
            "round_trip_count": len(self.round_trips),
        }


def _leg_amount(fill: dict) -> float:
    """체결금액. KIS 가 준 `amount`(tot_ccld_amt)가 있으면 **무조건 그것**을 쓰고,
    없을 때만 단가 x 수량으로 근사한다."""
    amt = fill.get("amount")
    if amt is not None:
        try:
            amt = float(amt)
        except (TypeError, ValueError):
            amt = 0.0
        if amt > 0:
            return amt
    price = float(fill.get("avg_price") or fill.get("price") or 0.0)
    return price * int(fill.get("quantity") or 0)


def _fill_sort_key(fill: dict):
    return (str(fill.get("timestamp") or ""), str(fill.get("order_id") or ""))


def realized_summary_from_fills(
    fills: Iterable[dict],
    *,
    symbol: Optional[str] = None,
    fee_total_override: Optional[float] = None,
    cost_engine: Optional[TradeCostEngine] = None,
) -> RealizedSummary:
    """체결 목록에서 FIFO 로 실현손익을 뽑는다.

    ``fee_total_override`` 를 주면(= KIS `prsm_tlex_smtl`) 그 값을 그대로 쓴다.
    KIS 가 알려준 실제 제비용이 언제나 우리 추정보다 우선한다.
    """
    engine = cost_engine or TradeCostEngine()
    rows = [f for f in fills if not symbol or str(f.get("symbol") or "") == symbol]
    rows.sort(key=_fill_sort_key)

    out = RealizedSummary(symbol=symbol or "")
    lots: deque = deque()          # 미청산 매수 [qty, 단가, order_id]
    fee_sum = 0.0
    tax_sum = 0.0
    rate = engine.realized_fee_rate()

    for f in rows:
        qty = int(f.get("quantity") or 0)
        if qty <= 0:
            continue
        amount = _leg_amount(f)
        side = str(f.get("side") or "").upper()
        sym = str(f.get("symbol") or symbol or "")
        # 레그별 원 단위 반올림 — KIS 표기 방식과 같다.
        fee_sum += round(amount * rate)
        if side == "SELL":
            tax_sum += round(amount * engine._tax_rate(sym, "SELL"))

        if side == "BUY":
            out.buy_qty += qty
            out.buy_amount += amount
            lots.append([qty, amount / qty, str(f.get("order_id") or "")])
            continue

        out.sell_qty += qty
        out.sell_amount += amount
        unit_sell = amount / qty
        remaining = qty
        while remaining > 0 and lots:
            lot = lots[0]
            take = min(remaining, lot[0])
            out.round_trips.append(RealizedRoundTrip(
                symbol=sym, quantity=take,
                buy_amount=lot[1] * take, sell_amount=unit_sell * take,
                buy_order_id=lot[2], sell_order_id=str(f.get("order_id") or ""),
            ))
            lot[0] -= take
            remaining -= take
            if lot[0] <= 0:
                lots.popleft()
        # 매수 없이 팔린 수량(전일 이월 등)은 매칭하지 않고 금액에만 반영된다.

    out.open_qty = sum(int(l[0]) for l in lots)
    out.open_amount = sum(l[0] * l[1] for l in lots)
    out.gross_pnl = sum(rt.gross_pnl for rt in out.round_trips)
    out.fee = float(fee_total_override) if fee_total_override is not None else fee_sum
    out.tax = tax_sum
    out.realized_pnl = out.gross_pnl - out.fee - out.tax
    return out


def reconcile_with_kis(
    fills: Iterable[dict], totals: Optional[dict] = None, *, symbol: Optional[str] = None,
) -> dict[str, Any]:
    """우리 계산과 KIS 집계(output2)를 대조한다.

    반환의 ``ok`` 가 False 면 **원장 숫자를 믿으면 안 된다** — 체결 누락이나
    요율 변경을 의심해야 한다.
    """
    rows = list(fills)
    kis_fee = None
    kis_amount = None
    if totals:
        try:
            kis_fee = float(totals.get("prsm_tlex_smtl"))
        except (TypeError, ValueError):
            kis_fee = None
        try:
            kis_amount = float(totals.get("tot_ccld_amt"))
        except (TypeError, ValueError):
            kis_amount = None

    ours = realized_summary_from_fills(rows, symbol=symbol)
    our_amount = ours.buy_amount + ours.sell_amount
    amount_diff = None if kis_amount is None else round(our_amount - kis_amount, 2)
    fee_diff = None if kis_fee is None else round(ours.fee - kis_fee, 2)

    truth = realized_summary_from_fills(rows, symbol=symbol, fee_total_override=kis_fee)
    return {
        "ok": (amount_diff in (None, 0.0)) and (fee_diff in (None, 0.0)),
        "our_total_amount": round(our_amount, 2), "kis_total_amount": kis_amount,
        "amount_diff": amount_diff,
        "our_fee": round(ours.fee, 2), "kis_fee": kis_fee, "fee_diff": fee_diff,
        "realized_pnl": round(truth.realized_pnl, 2),
        "summary": truth.as_dict(),
    }


# ── KIS 계좌 화면(TTTC8715R)을 그대로 읽는 경로 ────────────────────────────
#
# 위의 FIFO 계산은 **체결내역에서 손익을 재구성**하는 fallback 이다. KIS 가
# 기간별매매손익(TTTC8715R)을 주면 그쪽이 언제나 우선한다 — 그것이 사용자가
# 계좌 화면에서 보는 숫자 자체이기 때문이다. 우리가 요율로 추정한 값
# (`realized_fee_rate`)은 이 TR 을 쓸 수 없을 때(모의투자 등)만 쓰인다.

def _num(v, default=0.0) -> float:
    try:
        return float(str(v).replace(",", ""))
    except (TypeError, ValueError):
        return default


def account_realized_from_kis(totals: dict) -> dict[str, Any]:
    """TTTC8715R output2 를 UI 표시용으로 정규화한다. 추정 없음 — 전부 KIS 값.

    "매수금액"이 두 가지라 둘 다 돌려준다:
      buy_trade_amount     체결금액   (104,375)
      buy_settlement_amount 정산금액  (104,379 = 체결금액 + 매수수수료)
    계좌 화면의 "매수금액"은 **정산금액** 쪽이다.
    """
    t = totals or {}
    buy_trade = _num(t.get("buy_tr_amt_smtl"))
    sell_trade = _num(t.get("sll_tr_amt_smtl"))
    buy_fee = _num(t.get("buy_fee_smtl"))
    sell_fee = _num(t.get("sll_fee_smtl"))
    buy_tax = _num(t.get("buy_tax_smtl"))
    sell_tax = _num(t.get("sll_tltx_smtl"))
    buy_excc = _num(t.get("buy_excc_amt_smtl"))
    sell_excc = _num(t.get("sll_excc_amt_smtl"))
    return {
        "source": "KIS_TTTC8715R",
        "buy_qty": int(_num(t.get("buyqty_smtl"))),
        "sell_qty": int(_num(t.get("sll_qty_smtl"))),
        "buy_trade_amount": buy_trade,
        "sell_trade_amount": sell_trade,
        "buy_settlement_amount": buy_excc,
        "sell_settlement_amount": sell_excc,
        "buy_fee": buy_fee, "sell_fee": sell_fee,
        "fee": _num(t.get("tot_fee"), buy_fee + sell_fee),
        "tax": _num(t.get("tot_tltx"), buy_tax + sell_tax),
        "realized_pnl": _num(t.get("tot_rlzt_pfls")),
        "return_pct": _num(t.get("tot_pftrt")),
        "avg_buy_price": (buy_trade / int(_num(t.get("buyqty_smtl")))) if _num(t.get("buyqty_smtl")) else None,
        "avg_sell_price": (sell_trade / int(_num(t.get("sll_qty_smtl")))) if _num(t.get("sll_qty_smtl")) else None,
    }


def verify_kis_internal_consistency(totals: dict) -> dict[str, Any]:
    """KIS 가 준 숫자끼리 앞뒤가 맞는지 검산한다(우리 추정은 개입하지 않는다).

    셋 다 같은 값이 나와야 한다:
      체결금액 기준   매도체결 - 매수체결 - 총비용
      정산금액 기준   매도정산 - 매수정산
      KIS 보고값      tot_rlzt_pfls
    """
    a = account_realized_from_kis(totals)
    by_trade = a["sell_trade_amount"] - a["buy_trade_amount"] - a["fee"] - a["tax"]
    by_settlement = a["sell_settlement_amount"] - a["buy_settlement_amount"]
    reported = a["realized_pnl"]
    return {
        "ok": abs(by_trade - reported) < 1 and abs(by_settlement - reported) < 1,
        "by_trade_amount": round(by_trade, 2),
        "by_settlement_amount": round(by_settlement, 2),
        "kis_reported": reported,
        "buy_settlement_minus_trade": round(a["buy_settlement_amount"] - a["buy_trade_amount"], 2),
        "sell_trade_minus_settlement": round(a["sell_trade_amount"] - a["sell_settlement_amount"], 2),
    }
