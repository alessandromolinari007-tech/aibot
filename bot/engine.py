"""Loop principale: barre -> sweep -> validazione OFI/CVD -> rischio -> ordine limit
-> gestione (dampener HWM, uscita OFI, time exit) -> journal.

Resilienza ("self-healing" operativo):
  * stato del giorno, trade aperti e HWM del conto persistiti => riavvio sicuro;
  * riconciliazione con ordini/posizioni reali del broker a ogni ciclo;
  * retry con backoff e rate limiting sulle chiamate Alpaca (broker.py);
  * circuit breaker: troppi errori consecutivi => flat e stop;
  * kill switch: file state/KILL => chiude tutto e ferma il bot.
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
from .microstructure import check_absorption, ofi_pressure
from .risk import AccountGuard, DayState, Rejection, RiskManager, TradePlan
from .strategy import SweepCandidate, compute_levels, detect_sweep, parse_hhmm

log = logging.getLogger(__name__)

DAY_STATE_PATH = STATE_DIR / "day_state.json"
OPEN_TRADES_PATH = STATE_DIR / "open_trades.json"
ACCOUNT_PATH = STATE_DIR / "account.json"
GUARD_PATH = STATE_DIR / "account_guard.json"
KILL_PATH = STATE_DIR / "KILL"
MAX_CONSECUTIVE_ERRORS = 10
POLL_SECONDS = 5


def _status(order) -> str:
    return str(getattr(order.status, "value", order.status))


def _otype(order) -> str:
    return str(getattr(order.order_type, "value", order.order_type))


class TradingEngine:
    def __init__(self):
        STATE_DIR.mkdir(exist_ok=True)
        self.cfg = load_config()
        self.broker = AlpacaBroker(self.cfg)
        self.tz = self.cfg["session"]["timezone"]
        self.starting_equity = self._starting_equity()
        self.risk = RiskManager(self.cfg, self.starting_equity)
        self.guard = AccountGuard.load_or_new(GUARD_PATH, self.cfg, self.starting_equity)
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
        self.guard.save(GUARD_PATH)
        OPEN_TRADES_PATH.write_text(json.dumps(self.open_trades, indent=2, default=str))

    def _tunables_snapshot(self) -> dict:
        return {k: get_path(self.cfg, k) for k in self.cfg["coach"]["tunable"]}

    # ---------------------------------------------------------------- loop
    def run_forever(self) -> None:
        log.info("Bot avviato | profilo %s | equity di riferimento %.2f", self.cfg["prop_firm"], self.starting_equity)
        while True:
            try:
                if KILL_PATH.exists():
                    log.critical("KILL switch attivo: chiudo tutto e mi fermo.")
                    self.broker.flatten_all()
                    return
                if self.guard.breached:
                    log.critical("Max drawdown del conto violato: trading disabilitato. Vedi state/account_guard.json")
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
            if self.day is not None:
                self.guard.end_of_day(self.broker.equity())  # HWM EOD (Topstep MLL)
                self._persist()
                self.day = None  # al prossimo open si ricaricano config/override
            return
        if self.day is None or self.day.day != now.date().isoformat():
            self._start_day(now)

        equity = self.broker.equity()
        if self.guard.update(equity):
            log.critical("Equity %.2f sotto il floor %.2f: FLATTEN ALL", equity, self.guard.floor())
            self._flatten_everything("account_drawdown")
            return
        # DLL calcolato sull'equity (incluso PnL non realizzato), come FTMO.
        if equity - self.day.start_equity <= -self.risk.daily_loss_limit():
            self._flatten_everything("daily_loss_limit")
            self.day.halted_reason = "daily_loss_limit su equity"
            self._persist()
            return

        # [RICERCA] Profit clipping: con il cap di consistency raggiunto (incl. PnL aperto) => Flatten All.
        if self.cfg["consistency"]["enabled"] and not self.risk.consistency.evaluate_trading_state(
            equity - self.day.start_equity, self.risk.historical_profit(self.day)
        ):
            if self.open_trades or self.broker.positions():
                self._flatten_everything("consistency_cap")
            self.day.halted_reason = "Consistency cap raggiunto"

        self._manage_open_trades(now)
        if now.time() >= parse_hhmm(self.cfg["session"]["flatten_time"]):
            if self.open_trades or self.broker.positions():
                self._flatten_everything("time_exit")
            return
        for symbol in self.cfg["strategy"]["symbols"]:
            self._scan_symbol(symbol, now, equity)
        self._persist()

    def _start_day(self, now: pd.Timestamp) -> None:
        self.cfg = load_config()  # include gli override approvati dal coach
        self.risk = RiskManager(self.cfg, self.starting_equity)
        self.day = DayState.load_or_new(DAY_STATE_PATH, now.date(), self.broker.equity())
        log.info(
            "Nuovo giorno %s | equity %.2f | consistency cap %.2f | DLL %.2f | floor conto %.2f",
            self.day.day, self.day.start_equity, self.risk.daily_profit_cap(self.day),
            self.risk.daily_loss_limit(), self.guard.floor(),
        )

    def _flatten_everything(self, reason: str) -> None:
        log.warning("FLATTEN ALL (%s)", reason)
        self.broker.flatten_all()
        for t in self.open_trades.values():
            t["pending_exit_reason"] = reason
        self._persist()

    # -------------------------------------------------------------- entrate
    def _scan_symbol(self, symbol: str, now: pd.Timestamp, equity: float) -> None:
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
        cand = detect_sweep(symbol, df, levels, self.cfg["strategy"], self.cfg["session"], used)
        if cand is None:
            return

        s = self.cfg["strategy"]
        q_start = cand.sweep_start - pd.Timedelta(minutes=s["ofi_baseline_minutes"] + 1)
        absorption = check_absorption(
            cand.side, self.broker.quotes(symbol, q_start), self.broker.trades(symbol, cand.sweep_start),
            cand.sweep_start, s,
        )
        if not absorption.ok:
            log.info("Sweep %s su %s %s non validato: %s", cand.side, symbol, cand.level_name, absorption.reason)
            return
        plan = self.risk.plan(cand, absorption, equity, self.day)
        if isinstance(plan, Rejection):
            log.info("Sweep %s su %s %s scartato: %s", cand.side, symbol, cand.level_name, plan.reason)
            return
        self._enter(cand, absorption, plan, now)

    def _enter(self, cand: SweepCandidate, absorption, plan: TradePlan, now: pd.Timestamp) -> None:
        trade_id = f"nyls-{uuid.uuid4().hex[:12]}"
        order = self.broker.submit_limit_bracket(plan, client_order_id=trade_id)
        self.day.trades += 1
        self.day.used_levels.setdefault(plan.symbol, []).append(cand.level_name)
        record = {
            "event": "entry",
            "trade_id": trade_id,
            "order_id": str(order.id),
            "day": self.day.day,
            "time_et": cand.bar_time.strftime("%H:%M"),
            "symbol": plan.symbol,
            "side": plan.side,
            "level": cand.level_name,
            "level_price": cand.level_price,
            "sweep_extreme": cand.sweep_extreme,
            "penetration_atr": round(abs(cand.level_price - cand.sweep_extreme) / cand.atr, 3),
            "atr": round(cand.atr, 4),
            "ofi_ratio": round(absorption.ofi_ratio, 2),
            "ofi_norm": absorption.ofi_norm,
            "cvd_divergence": absorption.cvd_divergence,
            "poc": absorption.poc,
            "qty": plan.qty,
            "entry_limit": plan.entry_limit,
            "initial_stop": plan.stop,
            "take_profit": plan.take_profit,
            "risk_per_share": round(plan.risk_per_share, 4),
            "planned_r": plan.planned_r,
            "tp_clipped_by_consistency": plan.tp_clipped_by_consistency,
            "friction_share": plan.friction_share,
            "day_profit_cap": round(self.risk.daily_profit_cap(self.day), 2),
            "params": self._tunables_snapshot(),
        }
        journal.append(record)
        self.open_trades[trade_id] = {**asdict(plan), **record, "submitted_at": now.isoformat(),
                                      "hwm_peak": 0.0, "mfe_r": 0.0, "mae_r": 0.0}
        self._persist()
        log.info("LIMIT %s %s x%d @ %.2f stop %.2f tp %.2f (OFI x%.1f)", plan.side, plan.symbol, plan.qty,
                 plan.entry_limit, plan.stop, plan.take_profit, absorption.ofi_ratio)

    # ------------------------------------------------------- gestione/uscite
    def _manage_open_trades(self, now: pd.Timestamp) -> None:
        positions = self.broker.positions()
        for trade_id, t in list(self.open_trades.items()):
            order = self.broker.get_order(t["order_id"])
            filled_qty = float(order.filled_qty or 0)

            if filled_qty == 0:  # limit non ancora eseguito
                waited = (now - pd.Timestamp(t["submitted_at"])).total_seconds()
                if _status(order) in ("canceled", "expired", "rejected") or waited > self.cfg["execution"]["entry_timeout_seconds"]:
                    if _status(order) not in ("canceled", "expired", "rejected"):
                        self.broker.cancel_order(t["order_id"])
                    log.info("Limit %s %s non eseguito: cancellato", t["symbol"], trade_id)
                    journal.append({"event": "cancel", "trade_id": trade_id, "day": t["day"]})
                    self.day.trades = max(self.day.trades - 1, 0)  # un limit mai eseguito non consuma un tentativo
                    del self.open_trades[trade_id]
                continue

            entry = float(order.filled_avg_price)
            t["entry_fill"], t["qty"] = entry, int(filled_qty)
            legs = order.legs or []

            if "close_order_id" in t:  # chiusura software già inviata
                close = self.broker.get_order(t["close_order_id"])
                if close.filled_avg_price:
                    self._close_trade(trade_id, t, entry, float(close.filled_avg_price), t["pending_exit_reason"])
                continue
            filled_leg = next((leg for leg in legs if leg.filled_avg_price), None)
            if filled_leg is not None:
                reason = "take_profit" if _otype(filled_leg) == "limit" else "stop_loss"
                self._close_trade(trade_id, t, entry, float(filled_leg.filled_avg_price), reason)
                continue
            if t["symbol"] not in positions:
                # Chiusa da flatten_all / manualmente: prezzo approssimato all'ultimo trade.
                reason = t.get("pending_exit_reason", "external_or_manual")
                self._close_trade(trade_id, t, entry, self.broker.last_price(t["symbol"]), reason + "_approx")
                continue
            self._evaluate_exit(t, entry, legs, now)
        self._persist()

    def _evaluate_exit(self, t: dict, entry: float, legs, now: pd.Timestamp) -> None:
        price = self.broker.last_price(t["symbol"])
        sign = 1 if t["side"] == "long" else -1
        rps = abs(entry - t["initial_stop"]) or t["risk_per_share"]
        move_r = (price - entry) * sign / rps
        t["mfe_r"], t["mae_r"] = max(t["mfe_r"], move_r), min(t["mae_r"], move_r)
        unrealized = (price - entry) * sign * t["qty"]

        reason = None
        dampener = self.risk.dampener_for(t)
        if dampener.evaluate_tick_state(unrealized, now.to_pydatetime()):
            reason = "time_exit" if now.time() >= dampener.cutoff_time else "hwm_dampener"
        t["hwm_peak"] = dampener.hwm_peak

        s = self.cfg["strategy"]
        last_check = pd.Timestamp(t.get("last_ofi_check", t["submitted_at"]))
        if reason is None and move_r >= s["ofi_exit_min_r"] and (now - last_check).total_seconds() >= 60:
            t["last_ofi_check"] = now.isoformat()
            since = now - pd.Timedelta(minutes=s["ofi_exit_window_minutes"])
            pressure = ofi_pressure(t["side"], self.broker.quotes(t["symbol"], since), since, t["ofi_norm"])
            if pressure < s["ofi_neutral_threshold"]:
                reason = "ofi_neutral"  # [RICERCA] TP quando l'OFI torna neutrale / assorbimento avverso

        if reason:
            open_legs = [str(leg.id) for leg in legs if _status(leg) not in ("filled", "canceled", "expired")]
            close = self.broker.close_position(t["symbol"], open_legs)
            t["close_order_id"], t["pending_exit_reason"] = str(close.id), reason
            log.info("Uscita software %s %s (%s) a ~%.2f, %.2fR", t["symbol"], t["side"], reason, price, move_r)

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
        log.info("CHIUSO %s %s %s pnl %.2f (%.2fR)", t["symbol"], t["side"], reason, pnl, r_mult)
        block = self.risk.can_trade(self.day)
        if block and "tentativi" not in block.reason:
            self.day.halted_reason = block.reason
            log.info("Trading sospeso per oggi: %s", block.reason)
