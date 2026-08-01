from __future__ import annotations

from datetime import datetime
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.trading.tsla_auto import config, market_session, order_executor, state_store, worker
from app.trading.tsla_auto.models import Direction, PositionSnapshot
from tests.tsla_auto.fake_broker import FakeBroker


def _state_at(hour: int, minute: int):
    return market_session.get_us_market_state(datetime(2026, 8, 3, hour, minute, tzinfo=market_session.ET))


def main() -> None:
    broker = FakeBroker(cash_usd=100_000.0, quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0, "TSLQ": 40.0, "TSLY": 15.0})
    regular = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="replay-regular-buy",
        quotes={config.LONG_SYMBOL: 30.0}, position=None, budget_usd=10_000.0,
        market_state=_state_at(10, 0),
    )
    blocked = order_executor.execute_signal(
        broker=broker, direction=Direction.DOWN_BLUE, signal_id="replay-entry-blocked",
        quotes={config.LONG_SYMBOL: 30.0, config.INVERSE_SYMBOL: 12.0},
        position=PositionSnapshot(config.LONG_SYMBOL, regular.quantity, regular.filled_avg_price or 30.0),
        budget_usd=10_000.0, strategy_owned_qty=regular.quantity,
        market_state=_state_at(15, 45),
    )
    broker.buy_limit(config.LONG_SYMBOL, 10, 30.0, "managed-tsll")
    broker.buy_limit("TSLQ", 7, 40.0, "managed-tslq")
    broker.buy_limit("TSLY", 3, 15.0, "managed-tsly")
    broker.open_orders = [SimpleNamespace(order_id="BUY-OPEN-1", symbol=config.LONG_SYMBOL, side="BUY")]
    state = state_store.default_state()
    state.auto_trade_on = True
    result = worker.TickResult()
    worker._force_liquidate_managed_positions(
        broker=broker, state=state, now=datetime(2026, 8, 3, 15, 50, tzinfo=market_session.ET), result=result,
    )
    after = order_executor.execute_signal(
        broker=broker, direction=Direction.UP_RED, signal_id="replay-after-liquidation-buy",
        quotes={config.LONG_SYMBOL: 30.0}, position=None, budget_usd=10_000.0,
        market_state=_state_at(15, 50),
    )
    print(f"regular_buy={regular.final_state.value}:{regular.block_reason}")
    print(f"entry_blocked_reversal={blocked.final_state.value}:{blocked.block_reason}")
    print(f"cancelled_open_buys={broker.cancel_calls}")
    print(f"liquidation_actions={result.actions}")
    print(f"positions_after={[(p.symbol, p.quantity) for p in broker.get_positions()]}")
    print(f"post_liquidation_buy={after.final_state.value}:{after.block_reason}")


if __name__ == "__main__":
    main()
