# NY Liquidity Sweep Bot — self-healing, Alpaca paper trading

Sistema di trading intraday automatizzato con:

- **Entrata**: Liquidity Sweep dei livelli chiave (PDH/PDL, PMH/PML) nella killzone di apertura di New York, con conferma di reclaim e displacement.
- **Rischio**: stop dinamico ATR, sizing a rischio fisso, trailing ATR, limiti giornalieri e **Consistency Rule** (il take profit viene tagliato per non superare il budget di profitto del giorno).
- **Self-healing operativo**: stato persistito, riconciliazione dopo riavvio, retry con backoff, circuit breaker, kill switch.
- **Self-healing strategico**: `ai_coach.py` invia i log a Claude, riceve micro-aggiustamenti in JSON, li valida con guardrail deterministici e li annulla automaticamente (rollback) se peggiorano i risultati.

> ⚠️ **Il file `RICERCA.txt` ricevuto era vuoto.** Tutti i valori marcati `[RICERCA]` in `config.yaml` sono default conservativi di partenza: vanno sostituiti con i numeri del report (finestre orarie, soglie dello sweep, R:R, limiti giornalieri, quota massima per giorno della Consistency Rule, target del periodo).

## Architettura

```
run_bot.py            avvio del loop di trading
ai_coach.py           Fase 2: coach LLM (analisi trade -> override validati)
config.yaml           TUTTI i parametri; sezione coach.tunable = perimetro modificabile dall'AI
prompts/coach_system_prompt.md   prompt di sistema esatto inviato a Claude
bot/
  config.py           carica config.yaml + state/overrides.json (whitelist + clamp)
  strategy.py         livelli di liquidità, ATR, rilevazione sweep -> Signal
  risk.py             DayState, RiskManager (sizing, stop ATR, cap consistency, trailing)
  broker.py           wrapper Alpaca (dati, bracket order, replace stop) con retry
  engine.py           loop: segnale -> piano -> ordine -> gestione -> journal
  journal.py          logs/trades.jsonl + statistiche (expectancy R, best_day_share)
tests/test_core.py    test di strategia, rischio, guardrail del coach, rollback
```

Flusso di un giorno:

```
04:00-09:30  pre-market -> PMH/PML           (PDH/PDL dal giorno prima)
09:35-11:00  killzone: sweep + reclaim + displacement => bracket order (stop ATR, TP in R)
             trailing: breakeven a +1R, poi k*ATR; stop mai allargato
             stop di giornata: perdita max | profit cap raggiunto | N perdite consecutive
15:50        flatten di tutte le posizioni
16:30        ai_coach.py (cron): valuta prova precedente -> eventuale rollback -> nuova analisi
```

### La Consistency Rule in pratica

`profit_cap_oggi = min(target_giornaliero, safety_margin × max(share × target_periodo, share/(1−share) × profitto_accumulato))`

- Un trade viene dimensionato sul rischio (`risk_per_trade_pct`) e il suo TP viene **tagliato** se il profitto potenziale supererebbe il budget residuo del giorno.
- Se dopo il taglio il R:R scende sotto `min_rr_after_clip` il trade viene scartato.
- Raggiunto il cap, il bot smette di operare per il resto della giornata.

### Guardrail del coach (il modello non ha mai l'ultima parola)

| Guardrail | Default |
|---|---|
| Solo parametri in `coach.tunable`, sempre dentro `min`/`max` | — |
| Variazione massima per parametro per ciclo | ±20% |
| Modifiche massime per ciclo | 3 |
| Trade minimi prima di analizzare / valutare una prova | 10 |
| Ogni modifica deve citare trade a supporto | sì |
| Confidenza `low` → nessuna modifica | sì |
| Rollback se expectancy cala > 0.15R o il best day supera la quota consentita | sì |
| Errore API / risposta non valida → configurazione invariata | sì |

## Setup passo-passo

1. **Account Alpaca**: registrati su <https://alpaca.markets>, apri la dashboard **Paper Trading** e genera API Key + Secret.
2. **Chiave Claude API**: crea una chiave su <https://console.anthropic.com>.
3. **Ambiente Python** (3.11+):
   ```bash
   git clone <questo-repo> && cd aibot
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # poi inserisci le chiavi
   ```
4. **Calibra `config.yaml`** con i valori del report di ricerca (tutte le voci `[RICERCA]`). Se fai una valutazione prop firm, imposta `consistency.max_day_share` e `profit_target_pct` come da regolamento.
5. **Verifica**:
   ```bash
   python -m pytest -q
   python ai_coach.py --print-prompt    # mostra il prompt esatto che verrà inviato
   ```
6. **Avvia il bot** (lascialo girare prima dell'apertura USA, idealmente su un VPS):
   ```bash
   python run_bot.py
   ```
   Log in `logs/bot.log`, trade in `logs/trades.jsonl`.
7. **Pianifica il coach** dopo la chiusura (crontab, orario del server in UTC — 20:30 UTC ≈ 16:30 ET con ora legale):
   ```cron
   30 20 * * 1-5 cd /percorso/aibot && .venv/bin/python ai_coach.py >> logs/coach.log 2>&1
   ```
   Per le prime settimane usa `--dry-run` e leggi i report in `logs/coach_*.json` prima di lasciarlo applicare le modifiche.
8. **Servizio sempre attivo** (opzionale, systemd):
   ```ini
   [Service]
   WorkingDirectory=/percorso/aibot
   ExecStart=/percorso/aibot/.venv/bin/python run_bot.py
   Restart=always
   RestartSec=30
   ```
   Il riavvio è sicuro: stato del giorno e trade aperti vengono ricaricati da `state/`.

### Comandi operativi

| Azione | Comando |
|---|---|
| Stop di emergenza (chiude tutto) | `touch state/KILL` |
| Riprendere dopo il kill | `rm state/KILL` e riavvia |
| Annullare tutte le modifiche del coach | `rm state/overrides.json` |
| Vedere le modifiche attive e la storia | `cat state/overrides.json` |

## Il prompt per Claude

- **System prompt**: `prompts/coach_system_prompt.md` (i segnaposto `{max_changes}` e `{max_rel_change_pct}` vengono riempiti da `config.yaml`).
- **Messaggio utente**: generato da `build_user_message()` con statistiche, parametri modificabili con limiti, parametri fissi, storia delle modifiche e gli ultimi N trade, racchiusi in `<trading_data>`.
- **Output**: JSON vincolato da schema (structured outputs) con `diagnosis`, `consistency_assessment`, `adjustments[]` (parametro, valore attuale/nuovo, motivazione, trade a supporto, effetto atteso), `no_change_reason`, `risk_flags`, `confidence`.
- Modello `claude-opus-5-5` con thinking adattivo ed effort `high`; i fallback lato server sono attivi (`coach.use_server_fallbacks`) così, se il modello declina la richiesta, l'API la riesegue su un altro modello. Disattivali mettendo `false`.

Usa `python ai_coach.py --print-prompt` per vedere il prompt completo con i tuoi dati reali.

## Limiti noti

- Paper trading: niente slippage reale, fill idealizzati. Non passare a conto reale senza mesi di dati e una revisione indipendente (`broker.paper` è bloccato a `true` nel codice).
- Feed IEX gratuito: volumi parziali, quindi il filtro `min_volume_ratio` è approssimato. Con abbonamento SIP imposta `data_feed: sip`.
- Le uscite causate dal flatten di fine giornata o manuali registrano un prezzo approssimato (ultima chiusura a 5 minuti).
- Nessun backtest incluso: la taratura iniziale deve venire dal report di ricerca.
- Non è consulenza finanziaria.
