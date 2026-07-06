from abc import ABC, abstractmethod
from app.models import OrderResult, Position


class BrokerBase(ABC):
    mode: str  # "dry_run" | "mock" | "real"

    @abstractmethod
    def buy(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult: ...

    @abstractmethod
    def sell(
        self,
        symbol: str,
        name: str,
        quantity: int,
        price: float,
        order_type: str = "limit",
    ) -> OrderResult: ...

    @abstractmethod
    def get_positions(self) -> list[Position]: ...

    @abstractmethod
    def get_balance(self) -> float: ...

    @abstractmethod
    def get_current_price(self, symbol: str) -> float | None: ...

    @abstractmethod
    def get_buyable_cash(self) -> float: ...

    def is_real_mode(self) -> bool:
        return self.mode == "real"
