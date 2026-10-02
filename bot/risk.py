"""Architettura del rischio per Prop Firm [RICERCA: sezione "Architettura del Rischio"].

Le tre classi ConsistencyOptimizer, DynamicRiskSplitter e DrawdownDampenerAndTimer
riprendono la logica del report, adattata da contratti futures (tick * tick_value)
ad azioni (distanza di stop per azione). RiskManager le compone.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, time
from pathlib import Path

from .microstructure import Absorption
from .strategy import SweepCandidate


# --------------------------------------------------------------------------
# 1. Consistency Rule (Profit Clipping)
# --------------------------------------------------------------------------
class ConsistencyOptimizer:
    """Profit_Miglior_Giorno / Profitto_Netto_Totale <= threshold.

    hard_daily_cap = target * threshold; operativo = 90% (buffer slippage).
    Cap dinamico = max(operativo, (storico + oggi) * threshold * 0.90).
    """

    def __init__(self, target_profit: float, consistency_threshold: float = 0.40, buffer: float = 0.90):
        self.target_profit = target_profit
        self.consistency_threshold = consistency_threshold
        self.buffer = buffer
        self.hard_daily_cap = target_profit * consistency_threshold
        self.operational_daily_cap = self.hard_daily_cap * buffer

    def evaluate_trading_state(self, current_daily_pnl: float, total_historical_profit: float) -> bool:
        """True = si può operare; False = cap raggiunto, flatten e stop."""
        dynamic_daily_cap = max(
            self.operational_daily_cap,
            (total_historical_profit + current_daily_pnl) * self.consistency_threshold * self.buffer,
        )
        return current_daily_pnl < dynamic_daily_cap

    def max_daily_profit(self, total_historical_profit: float) -> float:
        """Profitto massimo di oggi prima che evaluate_trading_state diventi False.

        Risolve d = max(op_cap, (H + d) * k) con k = threshold * buffer
        => d = max(op_cap, k * H / (1 - k)). Usato per tagliare il take profit.
        """
        k = self.consistency_threshold * self.buffer
        h = max(total_historical_profit, 0.0)
        return max(self.operational_daily_cap, k * h / (1 - k)) if k < 1 else math.inf


# --------------------------------------------------------------------------
# 2. Volatility-Adjusted Risk Splitting (DLL)
# --------------------------------------------------------------------------
class DynamicRiskSplitter:
    """La size si riduce man mano che ci si avvicina al Daily Loss Limit."""

    def __init__(self, initial_balance: float, dll_percent: float, friction_buffer: float = 0.85):
        self.max_daily_loss = initial_balance * dll_percent
        self.usable_daily_budget = self.max_daily_loss * friction_buffer

    def remaining_budget(self, current_daily_loss: float) -> float:
        return self.usable_daily_budget - abs(min(0.0, current_daily_loss))

    def compute_position_size(self, current_daily_loss: float, risk_per_share: float, max_attempts: int = 3) -> int:
        remaining = self.remaining_budget(current_daily_loss)
        if remaining <= 0 or risk_per_share <= 0:
            return 0  # lock-out
        risk_per_trade = remaining / float(max_attempts)
        return int(math.floor(risk_per_trade / risk_per_share))


# --------------------------------------------------------------------------
# 3. HWM Dampener + Time-Based Exit
# --------------------------------------------------------------------------
class DrawdownDampenerAndTimer:
    """Trailing sul picco di PnL non realizzato del trade + uscita a orario."""

    def __init__(self, activation_threshold: float, trailing_distance: float, cutoff_time: time, hwm_peak: float = 0.0):
        self.activation_threshold = activation_threshold
        self.trailing_distance = trailing_distance
        self.cutoff_time = cutoff_time
        self.hwm_peak = hwm_peak

    def evaluate_tick_state(self, current_unrealized_pnl: float, current_server_time: datetime) -> bool:
        """True => 'Flatten' del trade."""
        if current_server_time.time() >= self.cutoff_time:
            return True
        self.hwm_peak = max(self.hwm_peak, current_unrealized_pnl)
        if self.hwm_peak >= self.activation_threshold:
            if self.hwm_peak - current_unrealized_pnl >= self.trailing_distance:
                return True
        return False


# --------------------------------------------------------------------------
# Drawdown massimo del conto (Topstep MLL / Apex trailing / FTMO statico)
# --------------------------------------------------------------------------
@dataclass
class AccountGuard:
    mode: str                 # static | intraday_trailing | eod_trailing_lock
    starting_equity: float
    max_drawdown: float       # in $
    hwm_intraday: float = 0.0
    hwm_eod: float = 0.0
    breached: bool = False

    def floor(self) -> float:
        if self.mode == "intraday_trailing":
            return max(self.hwm_intraday, self.starting_equity) - self.max_drawdown
        if self.mode == "eod_trailing_lock":
            # Segue il saldo di fine giornata e si blocca al valore iniziale.
            return min(max(self.hwm_eod, self.starting_equity) - self.max_drawdown, self.starting_equity)
        return self.starting_equity - self.max_drawdown

    def update(self, equity: float) -> bool:
        """Aggiorna l'HWM (equity incl. PnL non realizzato). True se violato."""
        self.hwm_intraday = max(self.hwm_intraday, equity)
        if equity <= self.floor():
            self.breached = True
        return self.breached

    def end_of_day(self, equity: float) -> None:
        self.hwm_eod = max(self.hwm_eod, equity)

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load_or_new(cls, path: Path, cfg: dict, starting_equity: float) -> "AccountGuard":
        dd = cfg["account_drawdown"]
        guard = cls(dd["mode"], starting_equity, starting_equity * dd["max_drawdown_pct"] / 100,
                    starting_equity, starting_equity)
        if path.exists():
            try:
                data = json.loads(path.read_text())
                guard.hwm_intraday = data.get("hwm_intraday", guard.hwm_intraday)
                guard.hwm_eod = data.get("hwm_eod", guard.hwm_eod)
                guard.breached = data.get("breached", False)
            except json.JSONDecodeError:
                pass
        return guard


# --------------------------------------------------------------------------
@dataclass
class DayState:
    day: str
    start_equity: float
    realized_pnl: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    used_levels: dict[str, list[str]] = field(default_factory=dict)
    halted_reason: str | None = None

    def register_close(self, pnl: float) -> None:
        self.realized_pnl += pnl
        self.consecutive_losses = self.consecutive_losses + 1 if pnl < 0 else 0

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(asdict(self), indent=2))

    @classmethod
    def load_or_new(cls, path: Path, today: date, equity: float) -> "DayState":
        """Self-healing: dopo un riavvio intraday riprende lo stato del giorno."""
        if path.exists():
            try:
                data = json.loads(path.read_text())
                if data.get("day") == today.isoformat():
                    return cls(**data)
            except (json.JSONDecodeError, TypeError):
                pass
        return cls(day=today.isoformat(), start_equity=equity)


@dataclass(frozen=True)
class TradePlan:
    symbol: str
    side: str
    qty: int
    entry_limit: float
    stop: float
    take_profit: float
    risk_per_share: float
    planned_r: float
    tp_clipped_by_consistency: bool
    friction_share: float


@dataclass(frozen=True)
class Rejection:
    reason: str


class RiskManager:
    def __init__(self, cfg: dict, starting_equity: float):
        self.r = cfg["risk"]
        self.c = cfg["consistency"]
        self.session = cfg["session"]
        self.starting_equity = starting_equity
        self.consistency = ConsistencyOptimizer(
            starting_equity * self.c["profit_target_pct"] / 100,
            self.c["threshold"],
            self.c["operational_buffer"],
        )
        self.splitter = DynamicRiskSplitter(
            starting_equity, self.r["daily_loss_limit_pct"] / 100, self.r["dll_friction_buffer"]
        )

    def historical_profit(self, day: DayState) -> float:
        return day.start_equity - self.starting_equity

    def daily_profit_cap(self, day: DayState) -> float:
        if not self.c["enabled"]:
            return math.inf
        return self.consistency.max_daily_profit(self.historical_profit(day))

    def daily_loss_limit(self) -> float:
        return self.splitter.max_daily_loss

    def can_trade(self, day: DayState) -> Rejection | None:
        if day.halted_reason:
            return Rejection(day.halted_reason)
        if day.trades >= self.r["max_attempts_per_day"]:
            return Rejection("tentativi giornalieri esauriti")
        if day.consecutive_losses >= self.r["max_consecutive_losses"]:
            return Rejection("max_consecutive_losses raggiunto")
        if self.splitter.remaining_budget(day.realized_pnl) <= 0:
            return Rejection("budget DLL esaurito (lock-out)")
        if self.c["enabled"] and not self.consistency.evaluate_trading_state(
            day.realized_pnl, self.historical_profit(day)
        ):
            return Rejection("Consistency cap raggiunto (profit clipping)")
        return None

    def plan(self, cand: SweepCandidate, absorption: Absorption, equity: float, day: DayState) -> TradePlan | Rejection:
        block = self.can_trade(day)
        if block:
            return block
        sign = 1 if cand.side == "long" else -1
        a = cand.atr
        # Ingresso limit sul nodo ad alta densità volumetrica (POC), mai peggiore dell'ultimo prezzo.
        entry = min(absorption.poc, cand.last_close) if sign == 1 else max(absorption.poc, cand.last_close)
        # Stop oltre la coda dello sweep, con distanza minima k*ATR.
        structural = cand.sweep_extreme - sign * self.r["stop_buffer_atr"] * a
        rps = max((entry - structural) * sign, self.r["atr_stop_mult"] * a)
        if rps <= 0 or (entry - cand.sweep_extreme) * sign <= 0:
            return Rejection("ingresso oltre l'estremo dello sweep")

        qty = self.splitter.compute_position_size(day.realized_pnl, rps, self.r["max_attempts_per_day"])
        qty = min(qty, math.floor(equity * self.r["max_position_notional_pct"] / 100 / entry))
        if qty < 1:
            return Rejection("quantità < 1 dopo il risk splitting")

        tp_dist = self.r["hard_take_profit_r"] * rps
        room = self.daily_profit_cap(day) - day.realized_pnl
        clipped = qty * tp_dist > room
        if clipped:
            tp_dist = room / qty
        planned_r = tp_dist / rps
        if planned_r < self.r["min_rr_after_clip"]:
            return Rejection(f"R:R {planned_r:.2f} < minimo dopo il profit clipping")
        friction_share = self.r["friction_per_share_rt"] / tp_dist
        if friction_share > self.r["max_friction_share_of_target"]:
            return Rejection(f"friction {friction_share:.0%} del target: edge mangiato dai costi")

        return TradePlan(
            symbol=cand.symbol,
            side=cand.side,
            qty=qty,
            entry_limit=round(entry, 2),
            stop=round(entry - sign * rps, 2),
            take_profit=round(entry + sign * tp_dist, 2),
            risk_per_share=rps,
            planned_r=round(planned_r, 3),
            tp_clipped_by_consistency=clipped,
            friction_share=round(friction_share, 4),
        )

    def dampener_for(self, trade: dict) -> DrawdownDampenerAndTimer:
        risk_dollars = trade["risk_per_share"] * trade["qty"]
        return DrawdownDampenerAndTimer(
            activation_threshold=self.r["dampener_activation_r"] * risk_dollars,
            trailing_distance=self.r["dampener_trailing_atr"] * trade["atr"] * trade["qty"],
            cutoff_time=time.fromisoformat(self.session["flatten_time"]),
            hwm_peak=trade.get("hwm_peak", 0.0),
        )
