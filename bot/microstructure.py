"""Metriche di microstruttura [RICERCA: OFI di Cont-de Larrard, CVD, volume profile].

Con Alpaca abbiamo solo il Livello 1 (best bid/ask) e i trade: è esattamente
ciò che serve alla formula OFI di Cont-de Larrard. Il feed IEX gratuito però
vede solo una parte del volume: le metriche sono quindi un'approssimazione.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


def ofi_events(quotes: pd.DataFrame) -> pd.Series:
    """OFI_t per ogni aggiornamento di quote.

    OFI_t = I{b_t >= b_t-1} q_b(t) - I{b_t <= b_t-1} q_b(t-1)
          - I{a_t <= a_t-1} q_a(t) + I{a_t >= a_t-1} q_a(t-1)
    """
    q = quotes[(quotes["bid_price"] > 0) & (quotes["ask_price"] > 0)]
    b, a = q["bid_price"], q["ask_price"]
    qb, qa = q["bid_size"].astype(float), q["ask_size"].astype(float)
    b1, a1, qb1, qa1 = b.shift(1), a.shift(1), qb.shift(1), qa.shift(1)
    e = (
        (b >= b1) * qb
        - (b <= b1) * qb1
        - (a <= a1) * qa
        + (a >= a1) * qa1
    )
    return e.iloc[1:].fillna(0.0)


def signed_volume(trades: pd.DataFrame, quotes: pd.DataFrame) -> pd.Series:
    """Delta per trade (Lee-Ready): + aggressore in acquisto, - in vendita."""
    t = trades[["price", "size"]].sort_index()
    q = quotes[["bid_price", "ask_price"]].sort_index()
    q = q[(q["bid_price"] > 0) & (q["ask_price"] > 0)]
    merged = pd.merge_asof(t, q, left_index=True, right_index=True, direction="backward")
    mid = (merged["bid_price"] + merged["ask_price"]) / 2
    sign = np.sign(merged["price"] - mid)
    tick = np.sign(merged["price"].diff()).replace(0, np.nan).ffill().fillna(0)
    sign = sign.where((sign != 0) & sign.notna(), tick)  # al mid: tick rule
    return sign * merged["size"].astype(float)


def point_of_control(trades: pd.DataFrame, bucket: float) -> float:
    """Prezzo con il massimo volume scambiato (nodo ad alta densità volumetrica)."""
    prices = (trades["price"] / bucket).round() * bucket
    return float(trades["size"].groupby(prices).sum().idxmax())


@dataclass(frozen=True)
class Absorption:
    ok: bool
    reason: str
    ofi_ratio: float = 0.0       # OFI per minuto nella finestra dello sweep / norma
    ofi_norm: float = 0.0        # |OFI| medio per minuto nella baseline
    cvd_divergence: bool = False
    poc: float = float("nan")


def check_absorption(
    side: str,
    quotes: pd.DataFrame,
    trades: pd.DataFrame,
    sweep_start: pd.Timestamp,
    cfg: dict,
) -> Absorption:
    """Distingue il 'fake-out' (stop run assorbito) dal breakout strutturale.

    LONG (sweep dei minimi): mentre il prezzo buca il livello l'OFI deve essere
    fortemente POSITIVO (ricarica passiva del bid) e il CVD non deve fare un
    minimo decrescente dopo l'estremo di prezzo (Absorption Divergence).
    SHORT: speculare.
    """
    if quotes.empty or trades.empty:
        return Absorption(False, "dati L1/trade assenti")
    direction = 1.0 if side == "long" else -1.0

    e = ofi_events(quotes)
    per_min = e.resample("1min").sum()
    baseline = per_min[per_min.index < sweep_start.floor("1min")].tail(cfg["ofi_baseline_minutes"])
    if len(baseline) < 3:
        return Absorption(False, "baseline OFI insufficiente")
    norm = float(baseline.abs().mean())
    if norm <= 0:
        return Absorption(False, "norma OFI nulla")

    window = e[e.index >= sweep_start]
    minutes = max((window.index.max() - sweep_start).total_seconds() / 60, 1.0) if len(window) else 1.0
    ratio = direction * float(window.sum()) / minutes / norm

    sw_trades = trades[trades.index >= sweep_start]
    if sw_trades.empty:
        return Absorption(False, "nessun trade nella finestra", ratio, norm)
    poc = point_of_control(sw_trades, cfg["poc_bucket"])

    cvd = signed_volume(sw_trades, quotes).cumsum()
    ext_pos = int(np.argmin(sw_trades["price"].values) if side == "long" else np.argmax(sw_trades["price"].values))
    after = cvd.iloc[ext_pos + 1 :]
    cvd_ok = bool(len(after) >= cfg["min_trades_after_extreme"]) and bool(
        after.min() >= cvd.iloc[ext_pos] if side == "long" else after.max() <= cvd.iloc[ext_pos]
    )

    if ratio < cfg["ofi_imbalance_factor"]:
        return Absorption(False, f"OFI ratio {ratio:.2f} < {cfg['ofi_imbalance_factor']}", ratio, norm, cvd_ok, poc)
    if cfg["require_cvd_absorption"] and not cvd_ok:
        return Absorption(False, "nessuna absorption divergence sul CVD", ratio, norm, cvd_ok, poc)
    return Absorption(True, "absorption confermata", ratio, norm, cvd_ok, poc)


def ofi_pressure(side: str, quotes: pd.DataFrame, since: pd.Timestamp, norm: float) -> float:
    """Pressione OFI recente nella direzione del trade, normalizzata (per l'uscita)."""
    e = ofi_events(quotes)
    e = e[e.index >= since]
    if e.empty or norm <= 0:
        return 0.0
    minutes = max((e.index.max() - since).total_seconds() / 60, 1.0)
    return (1.0 if side == "long" else -1.0) * float(e.sum()) / minutes / norm
