"""Trade journal (JSONL): è l'input del modulo ai_coach.py."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import ROOT

JOURNAL_PATH = ROOT / "logs" / "trades.jsonl"


def append(record: dict, path: Path = JOURNAL_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def read_trades(path: Path = JOURNAL_PATH, last_n: int | None = None) -> list[dict]:
    """Solo i trade chiusi (event == 'exit'), arricchiti con i dati di entrata."""
    if not path.exists():
        return []
    entries: dict[str, dict] = {}
    closed: list[dict] = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue  # riga troncata da un crash: si salta
        if rec.get("event") == "entry":
            entries[rec["trade_id"]] = rec
        elif rec.get("event") == "exit":
            closed.append({**entries.get(rec["trade_id"], {}), **rec})
    return closed[-last_n:] if last_n else closed


@dataclass(frozen=True)
class Stats:
    n: int
    win_rate: float
    expectancy_r: float
    total_pnl: float
    best_day_pnl: float
    best_day_share: float  # quota del giorno migliore sul profitto totale (Consistency)
    max_consecutive_losses: int


def compute_stats(trades: list[dict]) -> Stats:
    if not trades:
        return Stats(0, 0.0, 0.0, 0.0, 0.0, 0.0, 0)
    rs = [float(t.get("r_multiple", 0.0)) for t in trades]
    pnls = [float(t.get("pnl", 0.0)) for t in trades]
    by_day: dict[str, float] = {}
    for t, p in zip(trades, pnls):
        by_day[str(t.get("day"))] = by_day.get(str(t.get("day")), 0.0) + p
    total = sum(pnls)
    best = max(by_day.values())
    streak = worst = 0
    for p in pnls:
        streak = streak + 1 if p < 0 else 0
        worst = max(worst, streak)
    return Stats(
        n=len(trades),
        win_rate=round(sum(p > 0 for p in pnls) / len(pnls), 4),
        expectancy_r=round(sum(rs) / len(rs), 4),
        total_pnl=round(total, 2),
        best_day_pnl=round(best, 2),
        best_day_share=round(best / total, 4) if total > 0 else 0.0,
        max_consecutive_losses=worst,
    )
