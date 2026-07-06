from app.models import Position, OrderResult
from app.logger import logger
import json
from pathlib import Path
from datetime import datetime


class Portfolio:
    def __init__(self, initial_balance: float = 10_000_000):
        self._positions: dict[str, Position] = {}
        self._initial_balance: float = initial_balance
        self._cash: float = initial_balance
        self._order_counter: int = 0
        self._bought_symbols: set[str] = set()
        self._sold_symbols: set[str] = set()

    def add_position(self, result: OrderResult) -> None:
        if not result.success or result.side != "buy":
            return

        symbol = result.symbol
        qty = result.quantity
        price = result.price
        cost = price * qty

        if symbol in self._positions:
            existing = self._positions[symbol]
            total_qty = existing.quantity + qty
            total_cost = existing.avg_price * existing.quantity + cost
            existing.avg_price = total_cost / total_qty
            existing.quantity = total_qty
            existing.current_price = price
            logger.info(f"[Portfolio] 포지션 추가 매수: {symbol} {qty}주 @ {price:,.0f}원 (총 {total_qty}주)")
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                name=result.name,
                quantity=qty,
                avg_price=price,
                current_price=price,
                buy_order_id=result.order_id,
                opened_at=result.timestamp,
            )
            logger.info(f"[Portfolio] 신규 포지션: {symbol} {qty}주 @ {price:,.0f}원")

        self._cash -= cost
        self._bought_symbols.add(symbol)

    def remove_position(self, result: OrderResult) -> None:
        if not result.success or result.side != "sell":
            return

        symbol = result.symbol
        qty = result.quantity
        price = result.price

        if symbol not in self._positions:
            logger.warning(f"[Portfolio] 매도 처리 실패: {symbol} 포지션 없음")
            return

        existing = self._positions[symbol]
        proceeds = price * qty

        if qty >= existing.quantity:
            removed_qty = existing.quantity
            del self._positions[symbol]
            self._sold_symbols.add(symbol)
            logger.info(f"[Portfolio] 포지션 전량 청산: {symbol} {removed_qty}주 @ {price:,.0f}원")
        else:
            existing.quantity -= qty
            existing.current_price = price
            logger.info(f"[Portfolio] 포지션 일부 매도: {symbol} {qty}주 @ {price:,.0f}원 (잔여 {existing.quantity}주)")

        self._cash += proceeds

    def update_prices(self, prices: dict[str, float]) -> None:
        for symbol, price in prices.items():
            if symbol in self._positions:
                self._positions[symbol].current_price = price

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_cash(self) -> float:
        return self._cash

    def get_total_value(self) -> float:
        invested = sum(pos.market_value for pos in self._positions.values())
        return self._cash + invested

    def get_pnl(self) -> float:
        return self.get_total_value() - self._initial_balance

    def get_pnl_rate(self) -> float:
        if self._initial_balance == 0:
            return 0.0
        return self.get_pnl() / self._initial_balance * 100

    def is_duplicate_buy(self, symbol: str) -> bool:
        if symbol not in self._bought_symbols:
            return False
        # 완전히 청산된 경우 재매수 허용하지 않음 (sold_symbols에 있으면 중복 방지)
        # bought_symbols에 있고 아직 포지션이 있거나 sold_symbols에 있으면 중복
        return True

    def get_summary(self) -> dict:
        invested = sum(pos.market_value for pos in self._positions.values())
        total_value = self._cash + invested
        pnl = total_value - self._initial_balance
        pnl_rate = pnl / self._initial_balance * 100 if self._initial_balance else 0.0

        return {
            "initial_balance": self._initial_balance,
            "cash": self._cash,
            "invested": invested,
            "total_value": total_value,
            "pnl": pnl,
            "pnl_rate": pnl_rate,
            "n_positions": len(self._positions),
            "positions": [self._position_to_dict(pos) for pos in self._positions.values()],
        }

    def _position_to_dict(self, pos: Position) -> dict:
        return {
            "symbol": pos.symbol,
            "name": pos.name,
            "quantity": pos.quantity,
            "avg_price": pos.avg_price,
            "current_price": pos.current_price,
            "buy_order_id": pos.buy_order_id,
            "opened_at": pos.opened_at,
            "profit_rate": pos.profit_rate,
            "profit_amount": pos.profit_amount,
            "market_value": pos.market_value,
            "cost": pos.cost,
        }

    def to_dict(self) -> dict:
        return {
            "initial_balance": self._initial_balance,
            "cash": self._cash,
            "order_counter": self._order_counter,
            "bought_symbols": list(self._bought_symbols),
            "sold_symbols": list(self._sold_symbols),
            "positions": {
                symbol: self._position_to_dict(pos)
                for symbol, pos in self._positions.items()
            },
        }

    def from_dict(self, data: dict) -> None:
        self._initial_balance = data.get("initial_balance", self._initial_balance)
        self._cash = data.get("cash", self._initial_balance)
        self._order_counter = data.get("order_counter", 0)
        self._bought_symbols = set(data.get("bought_symbols", []))
        self._sold_symbols = set(data.get("sold_symbols", []))

        self._positions = {}
        for symbol, pos_data in data.get("positions", {}).items():
            self._positions[symbol] = Position(
                symbol=pos_data["symbol"],
                name=pos_data["name"],
                quantity=pos_data["quantity"],
                avg_price=pos_data["avg_price"],
                current_price=pos_data.get("current_price", pos_data["avg_price"]),
                buy_order_id=pos_data.get("buy_order_id", ""),
                opened_at=pos_data.get("opened_at", ""),
            )

    def save(self, filepath: str) -> None:
        path = Path(filepath)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)
        logger.info(f"[Portfolio] 저장 완료: {filepath}")

    def load(self, filepath: str) -> None:
        path = Path(filepath)
        if not path.exists():
            logger.warning(f"[Portfolio] 파일 없음, 초기 상태 유지: {filepath}")
            return
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        self.from_dict(data)
        logger.info(f"[Portfolio] 로드 완료: {filepath} (포지션 {len(self._positions)}개, 현금 {self._cash:,.0f}원)")
