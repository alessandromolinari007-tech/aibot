"""Entrata: Liquidity Sweep sui livelli chiave nella sessione di New York.

Logica (per un setup LONG; lo SHORT è speculare):
  1. Livelli di liquidità: minimo del giorno precedente (PDL) e minimo del
     pre-market (PML). Sotto questi livelli si accumulano gli stop dei long.
  2. Sweep: una barra della killzone buca il livello di almeno
     `sweep_min_penetration_atr` * ATR (ma non oltre `sweep_max_penetration_atr`,
     altrimenti è un breakout vero) con volume >= `min_volume_ratio` * media.
  3. Reclaim: entro `reclaim_max_bars` barre il prezzo CHIUDE di nuovo sopra il
     livello senza fare un nuovo minimo (la liquidità è stata presa e rifiutata).
  4. Displacement: la barra di reclaim è rialzista con corpo >= `displacement_body_atr` * ATR.
  => entrata long alla chiusura della barra di reclaim.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

import numpy as np
import pandas as pd

LOW_LEVELS = {"PDL", "PML"}
HIGH_LEVELS = {"PDH", "PMH"}


@dataclass(frozen=True)
class Signal:
    symbol: str
    side: str            # "long" | "short"
    level_name: str
    level_price: float
    sweep_extreme: float
    entry_ref: float
    atr: float
    bar_time: pd.Timestamp


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def atr(df: pd.DataFrame, period: int) -> pd.Series:
    """ATR di Wilder."""
    prev_close = df["close"].shift(1)
    tr = pd.concat(
        [df["high"] - df["low"], (df["high"] - prev_close).abs(), (df["low"] - prev_close).abs()],
        axis=1,
    ).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()


def compute_levels(df: pd.DataFrame, today: date, session_cfg: dict) -> dict[str, float]:
    """PDH/PDL dalla sessione regolare precedente, PMH/PML dal pre-market di oggi.

    `df` deve avere indice tz-aware nel fuso della sessione (America/New_York).
    """
    rth_open = parse_hhmm(session_cfg["rth_open"])
    rth_close = parse_hhmm(session_cfg["rth_close"])
    pm_start = parse_hhmm(session_cfg["premarket_start"])
    t = df.index.time
    d = df.index.date

    levels: dict[str, float] = {}
    rth = df[(t >= rth_open) & (t < rth_close) & (d < today)]
    if not rth.empty:
        prev_day = rth.index.date.max()
        prev = rth[rth.index.date == prev_day]
        levels["PDH"] = float(prev["high"].max())
        levels["PDL"] = float(prev["low"].min())
    pm = df[(d == today) & (t >= pm_start) & (t < rth_open)]
    if not pm.empty:
        levels["PMH"] = float(pm["high"].max())
        levels["PML"] = float(pm["low"].min())
    return levels


def in_window(ts: pd.Timestamp, start: str, end: str) -> bool:
    return parse_hhmm(start) <= ts.time() < parse_hhmm(end)


def detect_signal(
    symbol: str,
    df: pd.DataFrame,
    levels: dict[str, float],
    strat_cfg: dict,
    session_cfg: dict,
    used_levels: set[str] | None = None,
) -> Signal | None:
    """Valuta l'ULTIMA barra chiusa di `df` come possibile barra di reclaim."""
    used_levels = used_levels or set()
    period = strat_cfg["atr_period"]
    if len(df) < period + 21:
        return None

    atr_s = atr(df, period)
    vol_avg = df["volume"].rolling(20).mean().shift(1)
    i = len(df) - 1
    bar = df.iloc[i]
    ts = df.index[i]
    if not in_window(ts, session_cfg["killzone_start"], session_cfg["killzone_end"]):
        return None
    a = float(atr_s.iloc[i - 1]) if i > 0 else np.nan  # ATR noto prima della barra
    if not np.isfinite(a) or a <= 0:
        return None

    pen_min = strat_cfg["sweep_min_penetration_atr"] * a
    pen_max = strat_cfg["sweep_max_penetration_atr"] * a
    body = float(bar["close"] - bar["open"])
    max_back = int(strat_cfg["reclaim_max_bars"])
    allowed = set(strat_cfg["levels"])

    for name, lvl in levels.items():
        if name not in allowed:
            continue
        if strat_cfg.get("one_trade_per_level", True) and name in used_levels:
            continue
        is_low = name in LOW_LEVELS
        # Barra di reclaim: chiude dalla parte "giusta" con displacement.
        if is_low and not (bar["close"] > lvl and body >= strat_cfg["displacement_body_atr"] * a):
            continue
        if not is_low and not (bar["close"] < lvl and -body >= strat_cfg["displacement_body_atr"] * a):
            continue

        for s in range(i, max(i - max_back, 0) - 1, -1):
            sweep_bar = df.iloc[s]
            if not in_window(df.index[s], session_cfg["killzone_start"], session_cfg["killzone_end"]):
                break
            window = df.iloc[s : i + 1]
            if is_low:
                extreme = float(sweep_bar["low"])
                pen = lvl - extreme
                fresh = s > 0 and df.iloc[s - 1]["close"] > lvl
                is_extreme = extreme <= float(window["low"].min())
            else:
                extreme = float(sweep_bar["high"])
                pen = extreme - lvl
                fresh = s > 0 and df.iloc[s - 1]["close"] < lvl
                is_extreme = extreme >= float(window["high"].max())
            va = vol_avg.iloc[s]
            vol_ok = np.isfinite(va) and va > 0 and sweep_bar["volume"] >= strat_cfg["min_volume_ratio"] * va
            if fresh and is_extreme and pen_min <= pen <= pen_max and vol_ok:
                return Signal(
                    symbol=symbol,
                    side="long" if is_low else "short",
                    level_name=name,
                    level_price=float(lvl),
                    sweep_extreme=extreme,
                    entry_ref=float(bar["close"]),
                    atr=a,
                    bar_time=ts,
                )
    return None
