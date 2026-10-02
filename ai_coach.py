"""AI Coach "self-healing": analizza gli ultimi trade con Claude e applica
micro-aggiustamenti ai parametri, sempre dentro guardrail deterministici.

Ciclo (da lanciare dopo la chiusura, es. cron alle 16:30 ET):
  1. Valuta l'ultima modifica in prova: se l'expectancy è peggiorata => ROLLBACK.
  2. Se ci sono abbastanza trade, invia log + statistiche + parametri a Claude.
  3. Valida la risposta (whitelist, limiti min/max, variazione max, n. modifiche).
  4. Scrive state/overrides.json; il bot lo carica all'apertura successiva.

Uso:
  python ai_coach.py              # analizza e applica
  python ai_coach.py --dry-run    # analizza e mostra, senza applicare
  python ai_coach.py --print-prompt   # mostra il prompt esatto inviato a Claude
"""
from __future__ import annotations

import argparse
import json
import logging
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import anthropic
from dotenv import load_dotenv
from pydantic import BaseModel, ValidationError

from bot import journal
from bot.config import OVERRIDES_PATH, ROOT, clamp_value, get_path, load_config

log = logging.getLogger("ai_coach")
SYSTEM_PROMPT_PATH = ROOT / "prompts" / "coach_system_prompt.md"

# Campi del journal inviati al modello (niente rumore, niente order id).
TRADE_FIELDS = [
    "trade_id", "day", "time_et", "symbol", "side", "level", "penetration_atr", "atr",
    "planned_r", "tp_clipped_by_consistency", "exit_reason", "r_multiple", "pnl",
    "mfe_r", "mae_r", "day_realized_after", "day_profit_cap",
]


# ------------------------------------------------------------------ schema
class Adjustment(BaseModel):
    parameter: str
    current_value: float
    new_value: float
    rationale: str
    evidence_trade_ids: list[str]
    expected_effect: str


class CoachReport(BaseModel):
    diagnosis: str
    consistency_assessment: str
    adjustments: list[Adjustment]
    no_change_reason: str
    risk_flags: list[str]
    confidence: Literal["low", "medium", "high"]


def output_schema(tunable_keys: list[str]) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "diagnosis", "consistency_assessment", "adjustments",
            "no_change_reason", "risk_flags", "confidence",
        ],
        "properties": {
            "diagnosis": {"type": "string"},
            "consistency_assessment": {"type": "string"},
            "adjustments": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "parameter", "current_value", "new_value", "rationale",
                        "evidence_trade_ids", "expected_effect",
                    ],
                    "properties": {
                        "parameter": {"type": "string", "enum": tunable_keys},
                        "current_value": {"type": "number"},
                        "new_value": {"type": "number"},
                        "rationale": {"type": "string"},
                        "evidence_trade_ids": {"type": "array", "items": {"type": "string"}},
                        "expected_effect": {"type": "string"},
                    },
                },
            },
            "no_change_reason": {"type": "string"},
            "risk_flags": {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
        },
    }


# ------------------------------------------------------------ stato override
def load_state(path: Path = OVERRIDES_PATH) -> dict:
    if path.exists():
        try:
            return json.loads(path.read_text())
        except json.JSONDecodeError:
            log.error("overrides.json corrotto: riparto da zero")
    return {"active": {}, "history": []}


def save_state(state: dict, path: Path = OVERRIDES_PATH) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(path)  # scrittura atomica


def evaluate_trial(state: dict, all_trades: list[dict], coach_cfg: dict) -> str | None:
    """Accetta o annulla l'ultima modifica in prova confrontando l'expectancy (R)."""
    if not state["history"] or state["history"][-1]["status"] != "trial":
        return None
    last = state["history"][-1]
    after = all_trades[last["closed_trades_at_change"]:]
    if len(after) < coach_cfg["min_trades_required"]:
        return f"modifica in prova: {len(after)}/{coach_cfg['min_trades_required']} trade, attendo"
    new = journal.compute_stats(after)
    base_exp = last["baseline_stats"]["expectancy_r"]
    # Anche una Consistency peggiorata oltre il limite è motivo di rollback.
    limit = last.get("max_day_share", 1.0)
    consistency_worse = new.best_day_share > max(limit, last["baseline_stats"]["best_day_share"])
    if new.expectancy_r < base_exp - coach_cfg["rollback_if_expectancy_drops_r"] or consistency_worse:
        state["active"] = last["previous_active"]
        last["status"] = "rolled_back"
        last["result_stats"] = asdict(new)
        return f"ROLLBACK: expectancy {base_exp:.3f}R -> {new.expectancy_r:.3f}R, best_day_share {new.best_day_share:.2f}"
    last["status"] = "accepted"
    last["result_stats"] = asdict(new)
    return f"modifica accettata: expectancy {base_exp:.3f}R -> {new.expectancy_r:.3f}R"


# ------------------------------------------------------------------ prompt
def build_system_prompt(cfg: dict) -> str:
    c = cfg["coach"]
    return (
        SYSTEM_PROMPT_PATH.read_text()
        .replace("{max_changes}", str(c["max_changes_per_run"]))
        .replace("{max_rel_change_pct}", str(int(c["max_relative_change"] * 100)))
    )


def build_user_message(cfg: dict, trades: list[dict], state: dict) -> str:
    c = cfg["coach"]
    stats = journal.compute_stats(trades)
    tunables = {
        k: {"current": get_path(cfg, k), **{b: v for b, v in bounds.items()}}
        for k, bounds in c["tunable"].items()
    }
    fixed = {
        "risk.daily_loss_limit_pct": cfg["risk"]["daily_loss_limit_pct"],
        "risk.max_consecutive_losses": cfg["risk"]["max_consecutive_losses"],
        "risk.min_rr_after_clip": cfg["risk"]["min_rr_after_clip"],
        "consistency": {k: v for k, v in cfg["consistency"].items() if k != "starting_equity"},
        "session": cfg["session"],
        "symbols": cfg["strategy"]["symbols"],
    }
    history = [
        {k: h.get(k) for k in ("timestamp", "changes", "status", "baseline_stats", "result_stats")}
        for h in state["history"][-5:]
    ]
    compact = [{k: t.get(k) for k in TRADE_FIELDS} for t in trades]
    payload = {
        "summary_stats": asdict(stats),
        "consistency_limit_max_day_share": cfg["consistency"]["max_day_share"],
        "tunable_parameters": tunables,
        "fixed_parameters_do_not_change": fixed,
        "change_history": history,
        "trades": compact,
    }
    return (
        f"Ecco gli ultimi {len(trades)} trade chiusi e lo stato del sistema.\n"
        "Analizzali e proponi gli eventuali micro-aggiustamenti secondo le regole.\n\n"
        f"<trading_data>\n{json.dumps(payload, indent=2, default=str)}\n</trading_data>"
    )


# --------------------------------------------------------------- chiamata
def ask_claude(cfg: dict, system: str, user: str) -> CoachReport:
    c = cfg["coach"]
    client = anthropic.Anthropic()
    kwargs = dict(
        model=c["model"],
        max_tokens=16000,
        thinking={"type": "adaptive"},
        output_config={
            "effort": c["effort"],
            "format": {"type": "json_schema", "schema": output_schema(list(c["tunable"]))},
        },
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    if c.get("use_server_fallbacks"):
        # Se il modello primario declina, l'API riesegue su un modello di fallback.
        resp = client.beta.messages.create(
            betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs
        )
    else:
        resp = client.messages.create(**kwargs)

    if resp.stop_reason == "refusal":
        raise RuntimeError(f"Richiesta declinata dal modello: {resp.stop_details}")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Risposta troncata (max_tokens)")
    text = "".join(b.text for b in resp.content if b.type == "text")
    return CoachReport.model_validate_json(text)


# -------------------------------------------------------------- guardrail
def validate_adjustments(cfg: dict, report: CoachReport) -> tuple[dict, list[str]]:
    """Ritorna (modifiche approvate {param: valore}, note). Nessuna fiducia cieca nell'LLM."""
    c = cfg["coach"]
    approved: dict[str, float] = {}
    notes: list[str] = []
    if report.confidence == "low":
        return {}, ["confidenza 'low': nessuna modifica applicata"]
    for adj in report.adjustments:
        if len(approved) >= c["max_changes_per_run"]:
            notes.append(f"{adj.parameter}: scartato, superato max_changes_per_run")
            continue
        bounds = c["tunable"].get(adj.parameter)
        if bounds is None:
            notes.append(f"{adj.parameter}: non modificabile")
            continue
        if adj.parameter in approved:
            notes.append(f"{adj.parameter}: duplicato")
            continue
        if not adj.evidence_trade_ids:
            notes.append(f"{adj.parameter}: scartato, nessun trade a supporto")
            continue
        current = float(get_path(cfg, adj.parameter))
        max_step = abs(current) * c["max_relative_change"]
        if bounds.get("integer"):
            max_step = max(max_step, 1.0)  # un intero può sempre muoversi di 1
        target = min(max(adj.new_value, current - max_step), current + max_step)
        value = clamp_value(target, bounds)
        if value == current:
            notes.append(f"{adj.parameter}: nessuna variazione effettiva dopo i limiti")
            continue
        if value != adj.new_value:
            notes.append(f"{adj.parameter}: {adj.new_value} limitato a {value}")
        approved[adj.parameter] = value
    return approved, notes


# ------------------------------------------------------------------- main
def run(dry_run: bool = False, print_prompt: bool = False) -> int:
    cfg = load_config()
    c = cfg["coach"]
    state = load_state()
    all_trades = journal.read_trades()

    verdict = evaluate_trial(state, all_trades, c)
    if verdict:
        log.info(verdict)
        if not dry_run:
            save_state(state)
        if state["history"] and state["history"][-1]["status"] == "trial":
            return 0  # si aspetta la fine della prova prima di nuove modifiche
        cfg = load_config()  # il rollback può aver cambiato i valori attivi

    trades = all_trades[-c["lookback_trades"]:]
    system, user = build_system_prompt(cfg), build_user_message(cfg, trades, state)
    if print_prompt:
        print("=== SYSTEM ===\n" + system + "\n=== USER ===\n" + user)
        return 0
    if len(trades) < c["min_trades_required"]:
        log.info("Solo %d trade (< %d): nessuna analisi.", len(trades), c["min_trades_required"])
        return 0

    try:
        report = ask_claude(cfg, system, user)
    except (anthropic.APIError, RuntimeError, ValidationError) as exc:
        # Fail-safe: qualunque errore => la configurazione resta invariata.
        log.error("Coach non disponibile, nessuna modifica: %s", exc)
        return 1

    approved, notes = validate_adjustments(cfg, report)
    log.info("Diagnosi: %s", report.diagnosis)
    log.info("Consistency: %s", report.consistency_assessment)
    for f in report.risk_flags:
        log.warning("Risk flag: %s", f)
    for n in notes:
        log.info("Guardrail: %s", n)

    report_path = ROOT / "logs" / f"coach_{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.json"
    report_path.write_text(json.dumps({"report": report.model_dump(), "approved": approved, "notes": notes}, indent=2))

    if not approved:
        log.info("Nessuna modifica applicata. %s", report.no_change_reason)
        return 0
    if dry_run:
        log.info("[dry-run] modifiche approvate ma NON applicate: %s", approved)
        return 0

    state["history"].append(
        {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "changes": approved,
            "previous_active": dict(state["active"]),
            "baseline_stats": asdict(journal.compute_stats(trades)),
            "max_day_share": cfg["consistency"]["max_day_share"],
            "closed_trades_at_change": len(all_trades),
            "status": "trial",
            "report_file": report_path.name,
        }
    )
    state["active"] = {**state["active"], **approved}
    save_state(state)
    log.info("Override applicati (attivi dalla prossima sessione): %s", approved)
    return 0


def main() -> None:
    load_dotenv(ROOT / ".env")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--print-prompt", action="store_true")
    a = p.parse_args()
    raise SystemExit(run(dry_run=a.dry_run, print_prompt=a.print_prompt))


if __name__ == "__main__":
    main()
