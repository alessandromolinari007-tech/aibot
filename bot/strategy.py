"""Innesco: Liquidity Sweep dei livelli strutturali nell'apertura di New York.

[RICERCA] "Si identificano i supporti e le resistenze nodali pre-esistenti
(range overnight/asiatico o la prima candela di 5 minuti post-campanella).
Il trigger non è la reazione, ma la penetrazione meccanica del livello."

Questo modulo rileva la penetrazione (candidato). La conferma che si tratta di
uno stop run istituzionale e non di un breakout arriva da microstructure.py
(OFI + CVD). Nessun indicatore di prezzo tradizionale (MACD, medie mobili):
l'ATR serve solo a scalare soglie e stop alla volatilità.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, time

import numpy as np
import pandas as pd

LOW_LEVELS = {"ORL", "ONL", "PDL"}
HIGH_LEVELS = {"ORH", "ONH", "PDH"}


@dataclass(frozen=True)
class SweepCandidate:
    symbol: str
    side: str            # direzione del trade: "long" dopo sweep dei minimi
    level_name: str
    level_price: float
    sweep_extreme: float
    sweep_start: pd.Timestamp
    last_close: float
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
    """ORH/ORL (prima candela da N minuti), ONH/ONL (overnight/pre-market), PDH/PDL."""
    rth_open = parse_hhmm(session_cfg["rth_open"])
    rth_close = parse_hhmm(session_cfg["rth_close"])
    on_start = parse_hhmm(session_cfg["overnight_start"])
    or_end = (pd.Timestamp.combine(today, rth_open) + pd.Timedelta(minutes=session_cfg["opening_range_minutes"])).time()
    t, d = df.index.time, df.index.date

    levels: dict[str, float] = {}
    orng = df[(d == today) & (t >= rth_open) & (t < or_end)]
    # L'Opening Range vale solo quando la candela è completa.
    if not orng.empty and df[(d == today) & (t >= or_end)].shape[0] > 0:
        levels["ORH"], levels["ORL"] = float(orng["high"].max()), float(orng["low"].min())
    on = df[(d == today) & (t >= on_start) & (t < rth_open)]
    if not on.empty:
        levels["ONH"], levels["ONL"] = float(on["high"].max()), float(on["low"].min())
    rth = df[(t >= rth_open) & (t < rth_close) & (d < today)]
    if not rth.empty:
        prev = rth[rth.index.date == rth.index.date.max()]
        levels["PDH"], levels["PDL"] = float(prev["high"].max()), float(prev["low"].min())
    return levels


def in_window(ts: pd.Timestamp, start: str, end: str) -> bool:
    return parse_hhmm(start) <= ts.time() < parse_hhmm(end)


def detect_sweep(
    symbol: str,
    df: pd.DataFrame,
    levels: dict[str, float],
    strat_cfg: dict,
    session_cfg: dict,
    used_levels: set[str] | None = None,
) -> SweepCandidate | None:
    """Cerca una penetrazione 'da sweep' di un livello nelle ultime N barre chiuse."""
    used_levels = used_levels or set()
    period = strat_cfg["atr_period"]
    if len(df) < period + 2:
        return None
    i = len(df) - 1
    ts = df.index[i]
    if not in_window(ts, session_cfg["killzone_start"], session_cfg["killzone_end"]):
        return None
    a = float(atr(df, period).iloc[i - 1])
    if not np.isfinite(a) or a <= 0:
        return None

    pen_min = strat_cfg["sweep_min_penetration_atr"] * a
    pen_max = strat_cfg["sweep_max_penetration_atr"] * a
    start = max(i - strat_cfg["sweep_lookback_bars"] + 1, 1)
    window = df.iloc[start : i + 1]
    allowed = set(strat_cfg["levels"])

    for name, lvl in levels.items():
        if name not in allowed or (strat_cfg.get("one_trade_per_level", True) and name in used_levels):
            continue
        is_low = name in LOW_LEVELS
        extreme = float(window["low"].min() if is_low else window["high"].max())
        pen = (lvl - extreme) if is_low else (extreme - lvl)
        if not pen_min <= pen <= pen_max:
            continue
        breached = window["low"] < lvl if is_low else window["high"] > lvl
        first = int(np.argmax(breached.values))
        first_idx = start + first
        prev_close = float(df["close"].iloc[first_idx - 1])
        if (is_low and prev_close <= lvl) or (not is_low and prev_close >= lvl):
            continue  # il prezzo era già oltre il livello: non è uno sweep "fresco"
        return SweepCandidate(
            symbol=symbol,
            side="long" if is_low else "short",
            level_name=name,
            level_price=float(lvl),
            sweep_extreme=extreme,
            sweep_start=df.index[first_idx],
            last_close=float(df["close"].iloc[i]),
            atr=a,
            bar_time=ts,
        )
    return None
