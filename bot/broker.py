"""Wrapper Alpaca (alpaca-py) con retry/backoff per la resilienza di rete."""
from __future__ import annotations

import logging
import os
import time
from datetime import timedelta
from functools import wraps
from typing import Callable, TypeVar

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetOrderByIdRequest,
    MarketOrderRequest,
    ReplaceOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from .risk import TradePlan

log = logging.getLogger(__name__)
T = TypeVar("T")


def with_retry(attempts: int = 5, base_delay: float = 2.0) -> Callable:
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(*args, **kwargs) -> T:
            for k in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except (APIError, ConnectionError, TimeoutError, OSError) as exc:
                    status = getattr(exc, "status_code", None)
                    if status is not None and 400 <= status < 500 and status != 429:
                        raise  # errore del client: ritentare non serve
                    delay = base_delay * 2**k
                    log.warning("%s fallita (%s), retry tra %.0fs", fn.__name__, exc, delay)
                    time.sleep(delay)
            return fn(*args, **kwargs)

        return wrapper

    return deco


class AlpacaBroker:
    def __init__(self, cfg: dict):
        key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
        paper = cfg["broker"]["paper"]
        if not paper:
            raise RuntimeError("Questo sistema è configurato solo per paper trading.")
        self.trading = TradingClient(key, secret, paper=True)
        self.data = StockHistoricalDataClient(key, secret)
        self.feed = DataFeed(cfg["broker"]["data_feed"])
        self.tz = cfg["session"]["timezone"]
        self.tf_minutes = cfg["strategy"]["timeframe_minutes"]

    # ------------------------------------------------------------------ dati
    @with_retry()
    def bars(self, symbol: str, lookback_days: int = 5) -> pd.DataFrame:
        end = pd.Timestamp.now(tz="UTC").to_pydatetime()
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(self.tf_minutes, TimeFrameUnit.Minute),
            start=end - timedelta(days=lookback_days),
            end=end,
            feed=self.feed,
        )
        df = self.data.get_stock_bars(req).df
        if df.empty:
            return df
        df = df.xs(symbol, level="symbol") if isinstance(df.index, pd.MultiIndex) else df
        df.index = pd.DatetimeIndex(df.index).tz_convert(self.tz)
        # Scarta la barra ancora in formazione: usiamo solo barre CHIUSE.
        now = pd.Timestamp.now(tz=self.tz)
        closed = df.index + pd.Timedelta(minutes=self.tf_minutes) <= now
        return df.loc[closed, ["open", "high", "low", "close", "volume"]]

    # --------------------------------------------------------------- account
    @with_retry()
    def equity(self) -> float:
        return float(self.trading.get_account().equity)

    @with_retry()
    def clock(self):
        return self.trading.get_clock()

    @with_retry()
    def positions(self) -> dict[str, float]:
        return {p.symbol: float(p.qty) for p in self.trading.get_all_positions()}

    # ---------------------------------------------------------------- ordini
    @with_retry(attempts=2)
    def submit_bracket(self, plan: TradePlan, client_order_id: str):
        req = MarketOrderRequest(
            symbol=plan.symbol,
            qty=plan.qty,
            side=OrderSide.BUY if plan.side == "long" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            order_class=OrderClass.BRACKET,
            take_profit=TakeProfitRequest(limit_price=plan.take_profit),
            stop_loss=StopLossRequest(stop_price=plan.stop),
            client_order_id=client_order_id,
        )
        return self.trading.submit_order(req)

    @with_retry()
    def get_order(self, order_id: str):
        return self.trading.get_order_by_id(order_id, filter=GetOrderByIdRequest(nested=True))

    @with_retry(attempts=2)
    def replace_stop(self, stop_leg_id: str, new_stop: float):
        return self.trading.replace_order_by_id(stop_leg_id, ReplaceOrderRequest(stop_price=new_stop))

    @with_retry()
    def flatten_all(self) -> None:
        self.trading.cancel_orders()
        self.trading.close_all_positions(cancel_orders=True)
