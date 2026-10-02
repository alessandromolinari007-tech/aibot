"""Gestione del rischio: sizing, stop ATR, limiti giornalieri e Consistency Rule."""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from .strategy import Signal


@dataclass
class DayState:
    day: str
    start_equity: float
    realized_pnl: float = 0.0
    trades: int = 0
    consecutive_losses: int = 0
    used_levels: dict[str, list[str]] = field(default_factory=dict)  # symbol -> livelli usati
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
    entry_ref: float
    stop: float
    take_profit: float
    risk_per_share: float
    planned_r: float
    tp_clipped_by_consistency: bool


@dataclass(frozen=True)
class Rejection:
    reason: str


class RiskManager:
    def __init__(self, cfg: dict, starting_equity: float):
        self.r = cfg["risk"]
        self.c = cfg["consistency"]
        self.starting_equity = starting_equity

    # ------------------------------------------------------------------ limiti
    def daily_loss_limit(self, day: DayState) -> float:
        return day.start_equity * self.r["daily_loss_limit_pct"] / 100

    def daily_profit_cap(self, day: DayState) -> float:
        """Massimo profitto realizzabile oggi.

        min( target giornaliero , budget della Consistency Rule ).
        Consistency: best_day <= share * total_profit. Per non violarla:
          - all'inizio: oggi <= share * profit_target  (target del periodo)
          - più avanti: oggi <= share/(1-share) * profitto già accumulato
        si usa il maggiore dei due, ridotto dal margine di sicurezza.
        """
        target_cap = day.start_equity * self.r["daily_profit_target_pct"] / 100
        if not self.c["enabled"]:
            return target_cap
        share = self.c["max_day_share"]
        target_amount = self.starting_equity * self.c["profit_target_pct"] / 100
        cum_before = max(day.start_equity - self.starting_equity, 0.0)
        consistency_cap = max(share * target_amount, share / (1 - share) * cum_before)
        return min(target_cap, consistency_cap * self.c["safety_margin"])

    def can_trade(self, day: DayState) -> Rejection | None:
        if day.halted_reason:
            return Rejection(day.halted_reason)
        if day.trades >= self.r["max_trades_per_day"]:
            return Rejection("max_trades_per_day raggiunto")
        if day.consecutive_losses >= self.r["max_consecutive_losses"]:
            return Rejection("max_consecutive_losses raggiunto")
        if day.realized_pnl <= -self.daily_loss_limit(day):
            return Rejection("daily_loss_limit raggiunto")
        if day.realized_pnl >= self.daily_profit_cap(day):
            return Rejection("daily_profit_cap (target/consistency) raggiunto")
        return None

    # ------------------------------------------------------------------ piano
    def plan(self, sig: Signal, equity: float, day: DayState) -> TradePlan | Rejection:
        block = self.can_trade(day)
        if block:
            return block

        entry, a = sig.entry_ref, sig.atr
        atr_dist = self.r["atr_stop_mult"] * a
        buffer = self.r["stop_buffer_atr"] * a
        if sig.side == "long":
            structural = entry - (sig.sweep_extreme - buffer)
        else:
            structural = (sig.sweep_extreme + buffer) - entry
        rps = max(atr_dist, structural)  # stop oltre lo sweep e almeno k*ATR
        if rps <= 0:
            return Rejection("distanza stop non valida")

        # Rischio: % equity, ma mai oltre la perdita residua consentita oggi.
        risk_budget = equity * self.r["risk_per_trade_pct"] / 100
        loss_room = self.daily_loss_limit(day) + min(day.realized_pnl, 0.0)
        risk_amount = min(risk_budget, max(loss_room, 0.0))
        qty = math.floor(risk_amount / rps)
        max_notional = equity * self.r["max_position_notional_pct"] / 100
        qty = min(qty, math.floor(max_notional / entry))
        if qty < 1:
            return Rejection("quantità < 1 dopo il sizing")

        # Take profit in R, tagliato per non sforare il budget di consistency.
        tp_dist = self.r["take_profit_r"] * rps
        profit_room = self.daily_profit_cap(day) - day.realized_pnl
        clipped = False
        if qty * tp_dist > profit_room:
            tp_dist = profit_room / qty
            clipped = True
        planned_r = tp_dist / rps
        if planned_r < self.r["min_rr_after_clip"]:
            return Rejection(f"R:R {planned_r:.2f} < minimo dopo clip consistency")

        sign = 1 if sig.side == "long" else -1
        return TradePlan(
            symbol=sig.symbol,
            side=sig.side,
            qty=qty,
            entry_ref=entry,
            stop=round(entry - sign * rps, 2),
            take_profit=round(entry + sign * tp_dist, 2),
            risk_per_share=rps,
            planned_r=round(planned_r, 3),
            tp_clipped_by_consistency=clipped,
        )

    # ------------------------------------------------------------- trailing
    def trail_stop(
        self, side: str, entry: float, current_stop: float, rps: float, price: float, atr_now: float
    ) -> float:
        """Breakeven a +X R, poi trailing a k*ATR. Lo stop non si allarga mai."""
        sign = 1 if side == "long" else -1
        gain = (price - entry) * sign
        new_stop = current_stop
        if gain >= self.r["breakeven_at_r"] * rps:
            be = entry
            trail = price - sign * self.r["trail_atr_mult"] * atr_now
            candidate = max(be, trail) if sign == 1 else min(be, trail)
            new_stop = max(current_stop, candidate) if sign == 1 else min(current_stop, candidate)
        return round(new_stop, 2)
