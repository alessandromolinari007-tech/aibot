"""Loop principale: dati -> segnale -> rischio -> ordine -> gestione -> journal.

Resilienza ("self-healing" operativo):
  * stato del giorno e trade aperti persistiti su disco => riavvio sicuro intraday;
  * riconciliazione con le posizioni reali del broker all'avvio;
  * retry con backoff sulle chiamate di rete (broker.py);
  * circuit breaker: troppi errori consecutivi => flat e stop;
  * kill switch: crea il file state/KILL per chiudere tutto e fermare il bot.
"""
from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict
from pathlib import Path

import pandas as pd

from . import journal
from .broker import AlpacaBroker
from .config import STATE_DIR, get_path, load_config
from .risk import DayState, Rejection, RiskManager, TradePlan
from .strategy import atr, compute_levels, detect_signal, parse_hhmm

log = logging.getLogger(__name__)

DAY_STATE_PATH = STATE_DIR / "day_state.json"
OPEN_TRADES_PATH = STATE_DIR / "open_trades.json"
ACCOUNT_PATH = STATE_DIR / "account.json"
KILL_PATH = STATE_DIR / "KILL"
MAX_CONSECUTIVE_ERRORS = 10
POLL_SECONDS = 15


class TradingEngine:
    def __init__(self):
        STATE_DIR.mkdir(exist_ok=True)
        self.cfg = load_config()
        self.broker = AlpacaBroker(self.cfg)
        self.tz = self.cfg["session"]["timezone"]
        self.starting_equity = self._starting_equity()
        self.risk = RiskManager(self.cfg, self.starting_equity)
        self.day: DayState | None = None
        self.open_trades: dict[str, dict] = self._load_json(OPEN_TRADES_PATH, {})
        self.last_bar: dict[str, pd.Timestamp] = {}
        self.errors = 0

    # --------------------------------------------------------------- stato
    @staticmethod
    def _load_json(path: Path, default):
        try:
            return json.loads(path.read_text()) if path.exists() else default
        except json.JSONDecodeError:
            log.error("%s corrotto, riparto da default", path.name)
            return default

    def _starting_equity(self) -> float:
        configured = self.cfg["consistency"].get("starting_equity")
        if configured:
            return float(configured)
        saved = self._load_json(ACCOUNT_PATH, {})
        if "starting_equity" in saved:
            return float(saved["starting_equity"])
        eq = self.broker.equity()
        ACCOUNT_PATH.write_text(json.dumps({"starting_equity": eq}))
        return eq

    def _persist(self) -> None:
        if self.day:
            self.day.save(DAY_STATE_PATH)
        OPEN_TRADES_PATH.write_text(json.dumps(self.open_trades, indent=2, default=str))

    def _tunables_snapshot(self) -> dict:
        return {k: get_path(self.cfg, k) for k in self.cfg["coach"]["tunable"]}

    # ---------------------------------------------------------------- loop
    def run_forever(self) -> None:
        log.info("Bot avviato. Equity iniziale di riferimento: %.2f", self.starting_equity)
        while True:
            try:
                if KILL_PATH.exists():
                    log.critical("KILL switch attivo: chiudo tutto e mi fermo.")
                    self.broker.flatten_all()
                    return
                self.tick()
                self.errors = 0
            except Exception:  # noqa: BLE001 — il loop non deve mai morire in silenzio
                self.errors += 1
                log.exception("Errore nel ciclo (%d consecutivi)", self.errors)
                if self.errors >= MAX_CONSECUTIVE_ERRORS:
                    log.critical("Circuit breaker: troppi errori, flat e stop.")
                    try:
                        self.broker.flatten_all()
                    finally:
                        return
            time.sleep(POLL_SECONDS)

    def tick(self) -> None:
        clock = self.broker.clock()
        now = pd.Timestamp.now(tz=self.tz)
        if not clock.is_open:
            self.day = None  # al prossimo open si ricarica config/override
            return
        if self.day is None or self.day.day != now.date().isoformat():
            self._start_day(now)

        self._manage_open_trades(now)
        if now.time() >= parse_hhmm(self.cfg["session"]["flatten_time"]):
            if self.open_trades or self.broker.positions():
                log.info("Flatten di fine giornata")
                self.broker.flatten_all()
            return
        for symbol in self.cfg["strategy"]["symbols"]:
            self._scan_symbol(symbol, now)
        self._persist()

    def _start_day(self, now: pd.Timestamp) -> None:
        self.cfg = load_config()  # include gli override approvati dal coach
        self.risk = RiskManager(self.cfg, self.starting_equity)
        self.day = DayState.load_or_new(DAY_STATE_PATH, now.date(), self.broker.equity())
        log.info(
            "Nuovo giorno %s | equity %.2f | profit cap oggi %.2f | loss limit %.2f",
            self.day.day,
            self.day.start_equity,
            self.risk.daily_profit_cap(self.day),
            self.risk.daily_loss_limit(self.day),
        )

    # -------------------------------------------------------------- entrate
    def _scan_symbol(self, symbol: str, now: pd.Timestamp) -> None:
        if any(t["symbol"] == symbol for t in self.open_trades.values()):
            return
        if symbol in self.broker.positions():
            return  # posizione non tracciata (manuale?): non sovrapporsi
        df = self.broker.bars(symbol)
        if df.empty or self.last_bar.get(symbol) == df.index[-1]:
            return  # nessuna nuova barra chiusa
        self.last_bar[symbol] = df.index[-1]

        levels = compute_levels(df, now.date(), self.cfg["session"])
        used = set(self.day.used_levels.get(symbol, []))
        sig = detect_signal(symbol, df, levels, self.cfg["strategy"], self.cfg["session"], used)
        if sig is None:
            return
        plan = self.risk.plan(sig, self.broker.equity(), self.day)
        if isinstance(plan, Rejection):
            log.info("Segnale %s %s su %s scartato: %s", sig.side, symbol, sig.level_name, plan.reason)
            return
        self._enter(sig, plan)

    def _enter(self, sig, plan: TradePlan) -> None:
        trade_id = f"nyls-{uuid.uuid4().hex[:12]}"
        order = self.broker.submit_bracket(plan, client_order_id=trade_id)
        self.day.trades += 1
        self.day.used_levels.setdefault(plan.symbol, []).append(sig.level_name)
        ts = sig.bar_time
        record = {
            "event": "entry",
            "trade_id": trade_id,
            "order_id": str(order.id),
            "day": self.day.day,
            "time_et": ts.strftime("%H:%M"),
            "symbol": plan.symbol,
            "side": plan.side,
            "level": sig.level_name,
            "level_price": sig.level_price,
            "sweep_extreme": sig.sweep_extreme,
            "penetration_atr": round(abs(sig.level_price - sig.sweep_extreme) / sig.atr, 3),
            "atr": round(sig.atr, 4),
            "qty": plan.qty,
            "entry_ref": plan.entry_ref,
            "initial_stop": plan.stop,
            "take_profit": plan.take_profit,
            "risk_per_share": round(plan.risk_per_share, 4),
            "planned_r": plan.planned_r,
            "tp_clipped_by_consistency": plan.tp_clipped_by_consistency,
            "day_profit_cap": round(self.risk.daily_profit_cap(self.day), 2),
            "params": self._tunables_snapshot(),
        }
        journal.append(record)
        self.open_trades[trade_id] = {**asdict(plan), **record, "stop": plan.stop, "mfe_r": 0.0, "mae_r": 0.0}
        self._persist()
        log.info("ENTRATA %s %s x%d stop %.2f tp %.2f", plan.side, plan.symbol, plan.qty, plan.stop, plan.take_profit)

    # ------------------------------------------------------- gestione/uscite
    def _manage_open_trades(self, now: pd.Timestamp) -> None:
        for trade_id, t in list(self.open_trades.items()):
            order = self.broker.get_order(t["order_id"])
            status = str(order.status.value if hasattr(order.status, "value") else order.status)
            if status in ("canceled", "expired", "rejected") and not order.filled_qty:
                log.warning("Ordine %s non eseguito (%s): rimosso", trade_id, status)
                del self.open_trades[trade_id]
                continue
            if not order.filled_avg_price:
                continue
            entry = float(order.filled_avg_price)
            t["entry_fill"] = entry
            legs = order.legs or []
            filled_leg = next((leg for leg in legs if leg.filled_avg_price), None)
            still_open = t["symbol"] in self.broker.positions()

            if filled_leg is not None or not still_open:
                exit_price, reason = self._exit_info(t, filled_leg)
                self._close_trade(trade_id, t, entry, exit_price, reason)
                continue
            self._update_trailing(t, entry, legs)
        self._persist()

    def _exit_info(self, t: dict, leg) -> tuple[float, str]:
        if leg is not None:
            price = float(leg.filled_avg_price)
            if str(getattr(leg.order_type, "value", leg.order_type)) == "limit":
                return price, "take_profit"
            moved = abs(float(t["stop"]) - float(t["initial_stop"])) > 1e-9
            return price, "trailing_stop" if moved else "stop_loss"
        # Chiusura esterna (flatten di fine giornata / manuale): prezzo approssimato.
        df = self.broker.bars(t["symbol"], lookback_days=1)
        return float(df["close"].iloc[-1]), "eod_flatten_or_manual"

    def _close_trade(self, trade_id: str, t: dict, entry: float, exit_price: float, reason: str) -> None:
        sign = 1 if t["side"] == "long" else -1
        pnl = (exit_price - entry) * sign * t["qty"]
        rps = abs(entry - t["initial_stop"]) or t["risk_per_share"]
        r_mult = (exit_price - entry) * sign / rps
        self.day.register_close(pnl)
        journal.append(
            {
                "event": "exit",
                "trade_id": trade_id,
                "day": t["day"],
                "entry_fill": entry,
                "exit_price": exit_price,
                "exit_reason": reason,
                "pnl": round(pnl, 2),
                "r_multiple": round(r_mult, 3),
                "mfe_r": round(t.get("mfe_r", 0.0), 3),
                "mae_r": round(t.get("mae_r", 0.0), 3),
                "day_realized_after": round(self.day.realized_pnl, 2),
            }
        )
        del self.open_trades[trade_id]
        log.info("USCITA %s %s %s pnl %.2f (%.2fR)", t["symbol"], t["side"], reason, pnl, r_mult)
        block = self.risk.can_trade(self.day)
        if block and "trades" not in block.reason:
            self.day.halted_reason = block.reason
            log.info("Trading sospeso per oggi: %s", block.reason)

    def _update_trailing(self, t: dict, entry: float, legs) -> None:
        df = self.broker.bars(t["symbol"], lookback_days=3)
        if df.empty:
            return
        price = float(df["close"].iloc[-1])
        a = float(atr(df, self.cfg["strategy"]["atr_period"]).iloc[-1])
        sign = 1 if t["side"] == "long" else -1
        rps = abs(entry - t["initial_stop"]) or t["risk_per_share"]
        t["mfe_r"] = max(t.get("mfe_r", 0.0), (float(df["high" if sign == 1 else "low"].iloc[-1]) - entry) * sign / rps)
        t["mae_r"] = min(t.get("mae_r", 0.0), (float(df["low" if sign == 1 else "high"].iloc[-1]) - entry) * sign / rps)
        new_stop = self.risk.trail_stop(t["side"], entry, float(t["stop"]), rps, price, a)
        if new_stop == float(t["stop"]):
            return
        stop_leg = next(
            (leg for leg in legs if str(getattr(leg.order_type, "value", leg.order_type)) in ("stop", "stop_limit")),
            None,
        )
        if stop_leg is None:
            return
        self.broker.replace_stop(str(stop_leg.id), new_stop)
        log.info("Trailing %s: stop %.2f -> %.2f", t["symbol"], t["stop"], new_stop)
        t["stop"] = new_stop
