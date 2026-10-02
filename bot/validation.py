"""Igiene statistica [RICERCA: Deflated Sharpe Ratio, Triple Penance Rule].

Ogni modifica dei parametri fatta dal coach è un "esperimento" in più: più
varianti si provano, più è probabile trovare per caso un buon risultato
(Multiple Testing Problem). Il DSR penalizza lo Sharpe per il numero di
varianti testate, la skewness e la curtosi dei rendimenti.
"""
from __future__ import annotations

import math
from statistics import NormalDist

EULER_GAMMA = 0.5772156649
_N = NormalDist()


def moments(x: list[float]) -> tuple[float, float, float, float]:
    """media, deviazione standard, skewness, curtosi (non in eccesso)."""
    n = len(x)
    mu = sum(x) / n
    var = sum((v - mu) ** 2 for v in x) / (n - 1) if n > 1 else 0.0
    sd = math.sqrt(var)
    if sd == 0:
        return mu, 0.0, 0.0, 3.0
    m = [(v - mu) / sd for v in x]
    return mu, sd, sum(z**3 for z in m) / n, sum(z**4 for z in m) / n


def deflated_sharpe_ratio(returns: list[float], n_trials: int) -> float:
    """Probabilità che lo Sharpe osservato superi quello atteso dal puro caso.

    SR0 = sqrt(V[SR]) * ((1-γ) Φ⁻¹(1-1/N) + γ Φ⁻¹(1-1/(N e)))
    DSR = Φ( (SR - SR0) * sqrt(T-1) / sqrt(1 - γ3 SR + (γ4-1)/4 SR²) )
    V[SR] è stimata con la varianza dello stimatore dello Sharpe.
    """
    t = len(returns)
    if t < 3:
        return 0.0
    _, sd, g3, g4 = moments(returns)
    if sd == 0:
        return 0.0
    sr = sum(returns) / t / sd
    denom = 1 - g3 * sr + (g4 - 1) / 4 * sr**2
    if denom <= 0:
        return 0.0
    n = max(int(n_trials), 1)
    sr0 = 0.0
    if n > 1:
        var_sr = denom / (t - 1)
        sr0 = math.sqrt(var_sr) * (
            (1 - EULER_GAMMA) * _N.inv_cdf(1 - 1 / n) + EULER_GAMMA * _N.inv_cdf(1 - 1 / (n * math.e))
        )
    return _N.cdf((sr - sr0) * math.sqrt(t - 1) / math.sqrt(denom))


def drawdown_profile(returns: list[float]) -> tuple[float, int]:
    """(max drawdown in R, durata massima sott'acqua in numero di trade)."""
    peak = equity = 0.0
    max_dd, duration, max_duration = 0.0, 0, 0
    for r in returns:
        equity += r
        if equity >= peak:
            peak, duration = equity, 0
        else:
            duration += 1
            max_duration = max(max_duration, duration)
        max_dd = max(max_dd, peak - equity)
    return max_dd, max_duration
