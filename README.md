# NY Liquidity Sweep Bot — self-healing, Alpaca paper trading

Sistema di trading intraday automatizzato, costruito sul report `docs/RICERCA.txt`:

- **Entrata**: Liquidity Sweep dei livelli strutturali (Opening Range 5 min, range overnight) nella finestra 09:35–10:15 ET, **validato dal flusso ordini**: OFI di Cont–de Larrard ≥ 3× la norma contro la penetrazione + absorption divergence sul CVD. Ingresso con ordine **limit sul POC** (nodo di massimo volume).
- **Uscita**:
  - quando l'**OFI torna neutrale**;
  - **trailing sul picco di PnL** non realizzato (HWM dampener, distanza in ATR);
  - take profit di sicurezza;
  - uscita a orario.
- **Rischio da prop firm**:
  - le tre classi del report: `ConsistencyOptimizer`, `DynamicRiskSplitter`, `DrawdownDampenerAndTimer`;
  - Max Drawdown del conto secondo il profilo scelto (Topstep, Apex, FTMO);
  - filtro sulla friction.
- **Self-healing operativo**:
  - stato salvato su disco e riconciliazione con il broker;
  - retry con backoff e rate limiter (Alpaca ~200 req/min);
  - circuit breaker e kill switch.
- **Self-healing strategico**: `ai_coach.py` funziona così:
  1. invia i log a Claude e riceve micro-aggiustamenti in JSON;
  2. li valida con guardrail deterministici e con il **Deflated Sharpe Ratio**;
  3. li prova in **walk-forward 80/20** e fa **rollback** automatico se peggiorano.

## Dal report al codice

| Report (docs/RICERCA.txt) | Implementazione |
|---|---|
| Prima ora, operatività 09:30–10:15 EST | `session.killzone_*` (09:35 = dopo la prima candela da 5 min) |
| Livelli: Asian/overnight range, prima candela 5 min | `strategy.compute_levels` → ONH/ONL, ORH/ORL (PDH/PDL opzionali) |
| "Il trigger è la penetrazione meccanica del livello" | `strategy.detect_sweep` (penetrazione tra min e max in ATR, sweep "fresco") |
| OFI di Cont–de Larrard | `microstructure.ofi_events` (formula esatta sul Livello 1) |
| Sbilanciamento del 300% oltre la norma | `strategy.ofi_imbalance_factor: 3.0` |
| CVD senza minimo decrescente (Absorption Divergence) | `microstructure.check_absorption` (classificazione Lee-Ready) |
| Ingresso solo limit ai micro-livelli ad alta densità | limit bracket su `point_of_control`, cancellato dopo `entry_timeout_seconds` |
| Stop oltre la coda volumetrica | stop oltre l'estremo dello sweep + buffer, minimo `atr_stop_mult`×ATR |
| TP quando l'OFI torna neutrale | `engine._evaluate_exit` → `ofi_pressure < ofi_neutral_threshold` |
| ConsistencyOptimizer (40% XFA / 50% Combine, buffer 90%) | `risk.ConsistencyOptimizer` + profit clipping del TP + Flatten All al cap |
| DynamicRiskSplitter (85% del DLL / 3 tentativi) | `risk.DynamicRiskSplitter` |
| DrawdownDampenerAndTimer (HWM + cutoff) | `risk.DrawdownDampenerAndTimer`, distanza = `dampener_trailing_atr`×ATR×qty |
| Topstep MLL EOD bloccato / Apex trailing intraday / FTMO 5% + 10% | `prop_firm_profiles` + `risk.AccountGuard`; DLL calcolato sull'equity |
| Friction: 16–30% del lordo = rovina | scarto se `friction_per_share_rt / target > 10%` |
| Alpaca: ~200 req/min, notifiche lente | `RateLimiter` 180/min; stato ordini sempre riconciliato via polling |
| Walk-Forward 80/20 | coach: 40 trade in-sample per proporre, 10 out-of-sample per validare/annullare |
| Deflated Sharpe Ratio, multiple testing | `validation.deflated_sharpe_ratio` con N = configurazioni provate; sotto 0.95 il coach può solo ridurre il rischio |
| Triple Penance Rule | `Stats.triple_penance_recovery_trades`, passato al coach |

### Scostamenti obbligati dal report (limiti di Alpaca)

Il report stesso definisce Alpaca "inidoneo per lo scalping estremo su derivati". Questo sistema rispetta la logica del report, ma con questi limiti:

- **Niente futures**: ES/NQ sono sostituiti dagli ETF proxy **SPY/QQQ**.
- **Niente Livello 2 né tick CME**:
  - OFI e CVD sono calcolati su quote L1 e trade di Alpaca, che è proprio l'input della formula di Cont–de Larrard;
  - Queue Position e iceberg non sono osservabili;
  - con il feed gratuito **IEX** il volume è parziale. Con `data_feed: sip` (a pagamento) le metriche sono più affidabili.
- **Niente sessione asiatica sulle azioni**: il range overnight è approssimato con il pre-market 04:00–09:30 ET.
- **Latenza**: REST con polling ogni 5 secondi su barre da 1 minuto. Gli sweep di decine di millisecondi descritti nel report non sono catturabili: il sistema lavora sugli sweep che si sviluppano su una o più barre da 1 minuto.
- **Flatten**: alle 15:50 ET invece delle 15:10 CT di Topstep, perché le azioni chiudono alle 16:00 ET.

Per l'esecuzione istituzionale descritta nel report servono un altro gateway e un altro feed: Rithmic/Tradovate per i dati CME L2 e IBKR o Rithmic per gli ordini. La struttura del codice lo permette: basta sostituire `bot/broker.py`.

## Architettura

```
run_bot.py                      avvio del loop di trading
ai_coach.py                     coach LLM (analisi -> override validati -> walk-forward/rollback)
config.yaml                     tutti i parametri, profili prop firm, perimetro del coach
prompts/coach_system_prompt.md  prompt di sistema esatto inviato a Claude
docs/RICERCA.txt                report di ricerca
bot/
  config.py          carica config + profilo prop firm + state/overrides.json (whitelist + clamp)
  strategy.py        livelli, ATR, rilevazione sweep
  microstructure.py  OFI, CVD (Lee-Ready), POC, check_absorption, ofi_pressure
  risk.py            ConsistencyOptimizer, DynamicRiskSplitter, DrawdownDampenerAndTimer, AccountGuard, RiskManager
  validation.py      Deflated Sharpe Ratio, drawdown/underwater
  broker.py          Alpaca: barre, quote, trade, limit bracket, chiusure; retry + rate limit
  engine.py          loop: scan -> validate -> plan -> order -> manage -> journal
  journal.py         logs/trades.jsonl + statistiche
tests/test_core.py   17 test: strategia, OFI/CVD, classi del report, rischio, guardrail coach, rollback, DSR
```

Flusso di una giornata:

```
04:00-09:30  range overnight -> ONH/ONL
09:30-09:35  prima candela 5 min -> ORH/ORL
09:35-10:15  penetrazione di un livello -> OFI >= 3x norma contro lo sweep + CVD assorbito
             -> limit bracket sul POC (stop oltre la coda, TP di sicurezza tagliato dalla consistency)
in posizione OFI neutrale (dopo >= 0.5R) | HWM dampener | TP/SL bracket
sempre       equity <= floor drawdown -> FLATTEN e stop definitivo
             perdita del giorno (su equity) >= DLL -> FLATTEN e stop per oggi
             profitto del giorno >= cap consistency -> FLATTEN e stop per oggi
15:50        time exit
16:30        ai_coach.py (cron)
```

## Setup passo-passo

1. **Account Alpaca**: registrati su <https://alpaca.markets>, apri la dashboard **Paper Trading** e genera API Key + Secret.
2. **Chiave Claude API**: crea una chiave su <https://console.anthropic.com>.
3. **Ambiente Python** (3.11+):
   ```bash
   git clone <questo-repo> && cd aibot
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   cp .env.example .env   # inserisci le chiavi
   ```
4. **Scegli il profilo** in `config.yaml`: imposta `prop_firm` (`topstep_xfa`, `topstep_combine`, `apex`, `ftmo` o `none`) e `consistency.profit_target_pct` secondo il regolamento del tuo conto. Imposta `consistency.starting_equity` se vuoi simulare un conto di dimensione diversa dal paper account. I valori `[ASSUNZIONE]` vanno rivisti.
5. **Verifica**:
   ```bash
   python -m pytest -q
   python ai_coach.py --print-prompt    # mostra il prompt esatto che verrà inviato
   ```
6. **Avvia il bot** prima delle 09:30 ET, idealmente su un VPS:
   ```bash
   python run_bot.py
   ```
   Log in `logs/bot.log`, trade in `logs/trades.jsonl`.
7. **Pianifica il coach** dopo la chiusura (crontab in UTC: 20:30 UTC ≈ 16:30 ET con ora legale):
   ```cron
   30 20 * * 1-5 cd /percorso/aibot && .venv/bin/python ai_coach.py >> logs/coach.log 2>&1
   ```
   Per le prime settimane usa `--dry-run` e leggi i report in `logs/coach_*.json`.
8. **Servizio sempre attivo** (systemd, opzionale):
   ```ini
   [Service]
   WorkingDirectory=/percorso/aibot
   ExecStart=/percorso/aibot/.venv/bin/python run_bot.py
   Restart=always
   RestartSec=30
   ```

### Comandi operativi

| Azione | Comando |
|---|---|
| Stop di emergenza (chiude tutto) | `touch state/KILL` |
| Riprendere dopo il kill | `rm state/KILL` e riavvia |
| Annullare tutte le modifiche del coach | `rm state/overrides.json` |
| Ripartire dopo un drawdown violato (nuovo "conto") | `rm state/account_guard.json state/account.json` |

## Il coach AI e il prompt

- **System prompt**: `prompts/coach_system_prompt.md`. I segnaposto `{max_changes}` e `{max_rel_change_pct}` vengono riempiti da `config.yaml`.
- **Messaggio utente**, dentro `<trading_data>`:
  - statistiche (expectancy, best_day_share, skew/curtosi, drawdown, Triple Penance);
  - `overfitting_check` (N configurazioni, DSR, direzioni ammesse);
  - parametri modificabili con limiti e direzione `safer`;
  - parametri fissi (profilo prop firm, DLL, friction);
  - storia delle modifiche;
  - gli ultimi 40 trade, con i dati di microstruttura (`ofi_ratio`, `cvd_divergence`, `friction_share`).
- **Output**: JSON vincolato da schema (structured outputs) con i campi `diagnosis`, `consistency_assessment`, `adjustments[]`, `no_change_reason`, `risk_flags`, `confidence`.
- **Guardrail**, che il modello non può aggirare:
  - si modificano solo i parametri in whitelist, dentro min/max, al massimo ±20% e al massimo 2 modifiche per ciclo;
  - ogni modifica deve citare i trade a supporto;
  - con confidenza `low` non si applica nulla;
  - con **DSR < 0.95** sono ammesse solo modifiche che riducono il rischio;
  - se in prova sui 10 trade successivi l'expectancy peggiora di oltre 0.1R, o la consistency supera la soglia, scatta il **rollback**;
  - se l'API va in errore, la configurazione resta invariata.
- **Modello**: `claude-opus-5-5` con thinking adattivo ed effort `high`. I fallback lato server sono attivi (`coach.use_server_fallbacks`): se il modello declina, l'API riesegue la richiesta su un altro modello.

## Limiti noti

- Solo paper trading: `broker.paper` è bloccato a `true` nel codice.
- Nessun backtest né CPCV inclusi: la validazione è walk-forward sui trade reali di paper trading. Prima di usare capitale vero servono mesi di dati e una CPCV/PBO su dati tick (vedi report).
- Le uscite con suffisso `_approx` (flatten esterni) registrano un prezzo stimato.
- Non è consulenza finanziaria.
