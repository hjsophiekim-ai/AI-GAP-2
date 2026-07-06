import os
import logging
from datetime import datetime

import pandas as pd

logger = logging.getLogger(__name__)

try:
    from sqlalchemy import (
        create_engine, Column, Integer, String, Float, Boolean,
        DateTime, inspect, text
    )
    from sqlalchemy.orm import declarative_base, Session
    _SQLALCHEMY_AVAILABLE = True
except ImportError:
    logger.warning("SQLAlchemy not installed — SqliteStore will be disabled.")
    _SQLALCHEMY_AVAILABLE = False


if _SQLALCHEMY_AVAILABLE:
    Base = declarative_base()

    class OrderRecord(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True, autoincrement=True)
        date = Column(String)
        symbol = Column(String)
        name = Column(String)
        side = Column(String)
        quantity = Column(Integer)
        price = Column(Float)
        mode = Column(String)
        order_id = Column(String)
        success = Column(Boolean)
        message = Column(String)
        timestamp = Column(DateTime, default=datetime.now)

    class CandidateRecord(Base):
        __tablename__ = "candidates"
        id = Column(Integer, primary_key=True, autoincrement=True)
        date = Column(String)
        symbol = Column(String)
        name = Column(String)
        final_score = Column(Float)
        rank = Column(Integer)
        selected = Column(Boolean)


class SqliteStore:
    def __init__(self, db_path: str = "logs/ai_gap.db"):
        self._enabled = _SQLALCHEMY_AVAILABLE
        if not self._enabled:
            logger.warning("SqliteStore disabled: SQLAlchemy not available.")
            return

        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        abs_path = os.path.abspath(db_path)
        self._engine = create_engine(f"sqlite:///{abs_path}", echo=False)
        Base.metadata.create_all(self._engine)
        logger.debug("SqliteStore initialised: %s", abs_path)

    # ------------------------------------------------------------------
    def save_orders(self, orders: list) -> None:
        """Insert a list of order dicts into the orders table."""
        if not self._enabled:
            return
        if not orders:
            return
        try:
            records = [
                OrderRecord(
                    date=o.get("date"),
                    symbol=o.get("symbol"),
                    name=o.get("name"),
                    side=o.get("side"),
                    quantity=o.get("quantity"),
                    price=o.get("price"),
                    mode=o.get("mode"),
                    order_id=o.get("order_id"),
                    success=o.get("success"),
                    message=o.get("message"),
                    timestamp=o.get("timestamp", datetime.now()),
                )
                for o in orders
            ]
            with Session(self._engine) as session:
                session.add_all(records)
                session.commit()
            logger.debug("SqliteStore.save_orders: inserted %d rows", len(records))
        except Exception as exc:
            logger.warning("SqliteStore.save_orders failed: %s", exc)

    def save_candidates(self, candidates: list) -> None:
        """Insert a list of candidate dicts into the candidates table."""
        if not self._enabled:
            return
        if not candidates:
            return
        try:
            records = [
                CandidateRecord(
                    date=c.get("date"),
                    symbol=c.get("symbol"),
                    name=c.get("name"),
                    final_score=c.get("final_score"),
                    rank=c.get("rank"),
                    selected=c.get("selected"),
                )
                for c in candidates
            ]
            with Session(self._engine) as session:
                session.add_all(records)
                session.commit()
            logger.debug("SqliteStore.save_candidates: inserted %d rows", len(records))
        except Exception as exc:
            logger.warning("SqliteStore.save_candidates failed: %s", exc)

    def get_trade_history(self, date_str: str = None) -> pd.DataFrame:
        """Return orders as a DataFrame, optionally filtered by date."""
        if not self._enabled:
            return pd.DataFrame()
        try:
            with Session(self._engine) as session:
                query = session.query(OrderRecord)
                if date_str:
                    query = query.filter(OrderRecord.date == date_str)
                rows = query.all()

            if not rows:
                return pd.DataFrame()

            data = [
                {
                    "id": r.id,
                    "date": r.date,
                    "symbol": r.symbol,
                    "name": r.name,
                    "side": r.side,
                    "quantity": r.quantity,
                    "price": r.price,
                    "mode": r.mode,
                    "order_id": r.order_id,
                    "success": r.success,
                    "message": r.message,
                    "timestamp": r.timestamp,
                }
                for r in rows
            ]
            return pd.DataFrame(data)
        except Exception as exc:
            logger.warning("SqliteStore.get_trade_history failed: %s", exc)
            return pd.DataFrame()
