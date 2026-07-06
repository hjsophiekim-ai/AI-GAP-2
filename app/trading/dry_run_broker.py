import json
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from app.logger import logger
from app.models import OrderResult, Position
from app.trading.broker_base import BrokerBase

_DATA_DIR = Path("data/orders")


class DryRunBroker(BrokerBase):
    mode = "dry_run"

    def __init__(self, initial_balance: float = 10_000_000.0) -> None:
        self._initial_balance = initial_balance
        self._balance: float = initial_balance
        # symbol -> Position
        self._positions: dict[str, Position] = {}
        # symbols bought today (not yet sold) for duplicate-buy guard
        self._bought_today: set[str] = set()
        self._buy_counter: int = 0
        self._sell_counter: int = 0
        self._today: str = datetime.now().strftime("%Y%m%d")
        _DATA_DIR.mkdir(parents=True, exist_ok=True)
        self.load_state()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _state_path(self) -> Path:
        return _DATA_DIR / f"{self._today}_dry_portfolio.json"

    def _next_buy_id(self) -> str:
        self._buy_counter += 1
        return f"DRY-{self._today}-{self._buy_counter:04d}"

    def _next_sell_id(self) -> str:
        self._sell_counter += 1
        return f"DRY-SELL-{self._today}-{self._sell_counter:04d}"

    # ------------------------------------------------------------------
    # BrokerBase interface
    # ------------------------------------------------------------------

    def buy(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        if quantity < 1:
            return OrderResult(
                success=False,
                mode=self.mode,
                account_type="virtual",
                symbol=symbol,
                name=name,
                side="buy",
                quantity=quantity,
                price=price,
                order_type=order_type,
                order_id="",
                message="수량 부족",
            )

        if symbol in self._bought_today:
            return OrderResult(
                success=False,
                mode=self.mode,
                account_type="virtual",
                symbol=symbol,
                name=name,
                side="buy",
                quantity=quantity,
                price=price,
                order_type=order_type,
                order_id="",
                message="중복 매수 방지",
            )

        cost = price * quantity
        order_id = self._next_buy_id()

        if symbol in self._positions:
            pos = self._positions[symbol]
            total_qty = pos.quantity + quantity
            total_cost = pos.avg_price * pos.quantity + cost
            pos.avg_price = total_cost / total_qty
            pos.quantity = total_qty
        else:
            self._positions[symbol] = Position(
                symbol=symbol,
                name=name,
                quantity=quantity,
                avg_price=price,
                current_price=price,
                buy_order_id=order_id,
                opened_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            )

        self._balance -= cost
        self._bought_today.add(symbol)
        self.save_state()

        logger.info(
            f"[DryRun] BUY {name}({symbol}) {quantity}주 @{price:,.0f}원  "
            f"주문번호={order_id}  잔고={self._balance:,.0f}원"
        )

        return OrderResult(
            success=True,
            mode=self.mode,
            account_type="virtual",
            symbol=symbol,
            name=name,
            side="buy",
            quantity=quantity,
            price=price,
            order_type=order_type,
            order_id=order_id,
            message="dry-run 매수 완료",
        )

    def sell(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult:
        if symbol not in self._positions:
            return OrderResult(
                success=False,
                mode=self.mode,
                account_type="virtual",
                symbol=symbol,
                name=name,
                side="sell",
                quantity=quantity,
                price=price,
                order_type=order_type,
                order_id="",
                message="보유 종목 없음",
            )

        pos = self._positions[symbol]
        sell_qty = min(quantity, pos.quantity)
        order_id = self._next_sell_id()
        proceeds = price * sell_qty

        if sell_qty >= pos.quantity:
            del self._positions[symbol]
            self._bought_today.discard(symbol)
        else:
            pos.quantity -= sell_qty

        self._balance += proceeds
        self.save_state()

        logger.info(
            f"[DryRun] SELL {name}({symbol}) {sell_qty}주 @{price:,.0f}원  "
            f"주문번호={order_id}  잔고={self._balance:,.0f}원"
        )

        return OrderResult(
            success=True,
            mode=self.mode,
            account_type="virtual",
            symbol=symbol,
            name=name,
            side="sell",
            quantity=sell_qty,
            price=price,
            order_type=order_type,
            order_id=order_id,
            message="dry-run 매도 완료",
        )

    def get_positions(self) -> list[Position]:
        return list(self._positions.values())

    def get_balance(self) -> float:
        return self._balance

    def get_current_price(self, symbol: str) -> float | None:
        pos = self._positions.get(symbol)
        return pos.current_price if pos else None

    def get_buyable_cash(self) -> float:
        return self._balance

    # ------------------------------------------------------------------
    # State management
    # ------------------------------------------------------------------

    def reset(self) -> None:
        self._balance = self._initial_balance
        self._positions.clear()
        self._bought_today.clear()
        self._buy_counter = 0
        self._sell_counter = 0
        self._today = datetime.now().strftime("%Y%m%d")
        logger.info("[DryRun] 포트폴리오 초기화 완료")

    def save_state(self) -> None:
        state = {
            "date": self._today,
            "balance": self._balance,
            "buy_counter": self._buy_counter,
            "sell_counter": self._sell_counter,
            "bought_today": list(self._bought_today),
            "positions": [
                {
                    "symbol": p.symbol,
                    "name": p.name,
                    "quantity": p.quantity,
                    "avg_price": p.avg_price,
                    "current_price": p.current_price,
                    "buy_order_id": p.buy_order_id,
                    "opened_at": p.opened_at,
                }
                for p in self._positions.values()
            ],
        }
        path = self._state_path()
        path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        logger.debug(f"[DryRun] 상태 저장: {path}")

    def load_state(self) -> None:
        path = self._state_path()
        if not path.exists():
            logger.debug(f"[DryRun] 저장된 포트폴리오 없음: {path}")
            return
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("date") != self._today:
                logger.info("[DryRun] 날짜 변경 — 포트폴리오 초기화")
                return
            self._balance = state.get("balance", self._initial_balance)
            self._buy_counter = state.get("buy_counter", 0)
            self._sell_counter = state.get("sell_counter", 0)
            self._bought_today = set(state.get("bought_today", []))
            self._positions = {}
            for p in state.get("positions", []):
                self._positions[p["symbol"]] = Position(
                    symbol=p["symbol"],
                    name=p["name"],
                    quantity=p["quantity"],
                    avg_price=p["avg_price"],
                    current_price=p.get("current_price", p["avg_price"]),
                    buy_order_id=p.get("buy_order_id", ""),
                    opened_at=p.get("opened_at", ""),
                )
            logger.info(
                f"[DryRun] 포트폴리오 로드 완료: {len(self._positions)}종목  "
                f"잔고={self._balance:,.0f}원"
            )
        except Exception as exc:
            logger.warning(f"[DryRun] 포트폴리오 로드 실패: {exc}")
