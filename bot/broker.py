"""Wrapper Alpaca (alpaca-py): retry/backoff e rate limiting.

[RICERCA] Alpaca applica ~200 richieste/minuto per conto e può avere notifiche
lente nei picchi: per questo ogni chiamata passa da un rate limiter e lo stato
degli ordini viene sempre riconciliato via polling, non dato per scontato.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import timedelta
from functools import wraps
from typing import Callable, TypeVar

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import (
    StockBarsRequest,
    StockLatestTradeRequest,
    StockQuotesRequest,
    StockTradesRequest,
)
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetOrderByIdRequest,
    LimitOrderRequest,
    StopLossRequest,
    TakeProfitRequest,
)

from .risk import TradePlan

log = logging.getLogger(__name__)
T = TypeVar("T")


class RateLimiter:
    """Finestra scorrevole di 60 secondi."""

    def __init__(self, per_minute: int):
        self.per_minute = per_minute
        self.calls: deque[float] = deque()
        self.lock = threading.Lock()

    def acquire(self) -> None:
        with self.lock:
            now = time.monotonic()
            while self.calls and now - self.calls[0] > 60:
                self.calls.popleft()
            if len(self.calls) >= self.per_minute:
                wait = 60 - (now - self.calls[0]) + 0.05
                log.warning("Rate limit Alpaca: attendo %.1fs", wait)
                time.sleep(wait)
            self.calls.append(time.monotonic())


def with_retry(attempts: int = 5, base_delay: float = 2.0) -> Callable:
    def deco(fn: Callable[..., T]) -> Callable[..., T]:
        @wraps(fn)
        def wrapper(self, *args, **kwargs) -> T:
            for k in range(attempts):
                try:
                    self.limiter.acquire()
                    return fn(self, *args, **kwargs)
                except (APIError, ConnectionError, TimeoutError, OSError) as exc:
                    status = getattr(exc, "status_code", None)
                    if status is not None and 400 <= status < 500 and status != 429:
                        raise  # errore del client: ritentare non serve
                    if k == attempts - 1:
                        raise
                    delay = base_delay * 2**k
                    log.warning("%s fallita (%s), retry tra %.0fs", fn.__name__, exc, delay)
                    time.sleep(delay)
            raise RuntimeError("unreachable")

        return wrapper

    return deco


def _single_symbol(df: pd.DataFrame, symbol: str, tz: str) -> pd.DataFrame:
    if df.empty:
        return df
    df = df.xs(symbol, level="symbol") if isinstance(df.index, pd.MultiIndex) else df
    df.index = pd.DatetimeIndex(df.index).tz_convert(tz)
    return df.sort_index()


class AlpacaBroker:
    def __init__(self, cfg: dict):
        key, secret = os.environ["ALPACA_API_KEY"], os.environ["ALPACA_SECRET_KEY"]
        if not cfg["broker"]["paper"]:
            raise RuntimeError("Questo sistema è configurato solo per paper trading.")
        self.trading = TradingClient(key, secret, paper=True)
        self.data = StockHistoricalDataClient(key, secret)
        self.feed = DataFeed(cfg["broker"]["data_feed"])
        self.tz = cfg["session"]["timezone"]
        self.tf_minutes = cfg["strategy"]["timeframe_minutes"]
        self.limiter = RateLimiter(cfg["broker"]["rate_limit_per_minute"])

    # ------------------------------------------------------------------ dati
    @with_retry()
    def bars(self, symbol: str, lookback_days: int = 4) -> pd.DataFrame:
        end = pd.Timestamp.now(tz="UTC").to_pydatetime()
        req = StockBarsRequest(
            symbol_or_symbols=symbol,
            timeframe=TimeFrame(self.tf_minutes, TimeFrameUnit.Minute),
            start=end - timedelta(days=lookback_days),
            end=end,
            feed=self.feed,
        )
        df = _single_symbol(self.data.get_stock_bars(req).df, symbol, self.tz)
        if df.empty:
            return df
        now = pd.Timestamp.now(tz=self.tz)
        closed = df.index + pd.Timedelta(minutes=self.tf_minutes) <= now  # solo barre CHIUSE
        return df.loc[closed, ["open", "high", "low", "close", "volume"]]

    @with_retry()
    def quotes(self, symbol: str, start: pd.Timestamp) -> pd.DataFrame:
        req = StockQuotesRequest(symbol_or_symbols=symbol, start=start.to_pydatetime(), feed=self.feed)
        return _single_symbol(self.data.get_stock_quotes(req).df, symbol, self.tz)

    @with_retry()
    def trades(self, symbol: str, start: pd.Timestamp) -> pd.DataFrame:
        req = StockTradesRequest(symbol_or_symbols=symbol, start=start.to_pydatetime(), feed=self.feed)
        return _single_symbol(self.data.get_stock_trades(req).df, symbol, self.tz)

    @with_retry()
    def last_price(self, symbol: str) -> float:
        req = StockLatestTradeRequest(symbol_or_symbols=symbol, feed=self.feed)
        return float(self.data.get_stock_latest_trade(req)[symbol].price)

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
    def submit_limit_bracket(self, plan: TradePlan, client_order_id: str):
        req = LimitOrderRequest(
            symbol=plan.symbol,
            qty=plan.qty,
            side=OrderSide.BUY if plan.side == "long" else OrderSide.SELL,
            limit_price=plan.entry_limit,
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
    def cancel_order(self, order_id: str) -> None:
        self.trading.cancel_order_by_id(order_id)

    @with_retry(attempts=3)
    def close_position(self, symbol: str, open_leg_ids: list[str]):
        """Cancella le gambe del bracket (che bloccano le azioni) e chiude a mercato."""
        for leg_id in open_leg_ids:
            try:
                self.trading.cancel_order_by_id(leg_id)
            except APIError as exc:
                log.warning("Cancel gamba %s: %s", leg_id, exc)
        return self.trading.close_position(symbol)

    @with_retry()
    def flatten_all(self) -> None:
        self.trading.cancel_orders()
        self.trading.close_all_positions(cancel_orders=True)
