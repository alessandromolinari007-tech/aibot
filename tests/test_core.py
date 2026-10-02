import copy
from datetime import date, datetime, time

import pandas as pd
import pytest

import ai_coach
from bot.config import apply_overrides, load_config
from bot.journal import compute_stats
from bot.microstructure import Absorption, check_absorption, ofi_events, point_of_control
from bot.risk import (
    AccountGuard,
    ConsistencyOptimizer,
    DayState,
    DrawdownDampenerAndTimer,
    DynamicRiskSplitter,
    Rejection,
    RiskManager,
    TradePlan,
)
from bot.strategy import SweepCandidate, compute_levels, detect_sweep
from bot.validation import deflated_sharpe_ratio

CFG = load_config(with_overrides=False)  # profilo topstep_xfa
TZ = "America/New_York"
DAY = "2026-10-01"


# ------------------------------------------------------------------ strategia
def make_bars():
    rows = []
    for ts in pd.date_range("2026-09-30 09:30", "2026-09-30 15:59", freq="1min", tz=TZ):
        rows.append((ts, 100.5, 101.0, 100.0, 100.5, 1000))                 # PDH 101 / PDL 100
    for ts in pd.date_range(f"{DAY} 04:00", f"{DAY} 09:29", freq="1min", tz=TZ):
        rows.append((ts, 100.5, 100.8, 100.2, 100.5, 200))                  # ONH 100.8 / ONL 100.2
    for ts in pd.date_range(f"{DAY} 09:30", f"{DAY} 09:34", freq="1min", tz=TZ):
        rows.append((ts, 100.5, 100.7, 100.3, 100.5, 3000))                 # ORH 100.7 / ORL 100.3
    for ts in pd.date_range(f"{DAY} 09:35", f"{DAY} 09:39", freq="1min", tz=TZ):
        rows.append((ts, 100.5, 100.6, 100.4, 100.5, 2000))
    rows.append((pd.Timestamp(f"{DAY} 09:40", tz=TZ), 100.5, 100.5, 100.25, 100.35, 5000))  # sweep di ORL
    return pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")


def test_levels():
    lv = compute_levels(make_bars(), date(2026, 10, 1), CFG["session"])
    assert lv == {"ORH": 100.7, "ORL": 100.3, "ONH": 100.8, "ONL": 100.2, "PDH": 101.0, "PDL": 100.0}


def test_detect_sweep_of_opening_range_low():
    df = make_bars()
    lv = compute_levels(df, date(2026, 10, 1), CFG["session"])
    c = detect_sweep("SPY", df, lv, CFG["strategy"], CFG["session"])
    assert c is not None and c.side == "long" and c.level_name == "ORL"
    assert c.sweep_extreme == 100.25 and c.sweep_start == pd.Timestamp(f"{DAY} 09:40", tz=TZ)
    # livello già usato o fuori killzone => niente
    assert detect_sweep("SPY", df, lv, CFG["strategy"], CFG["session"], {"ORL"}) is None
    late = df.copy()
    late.index = late.index + pd.Timedelta("1h")
    assert detect_sweep("SPY", late, lv, CFG["strategy"], CFG["session"]) is None


# ------------------------------------------------------------- microstruttura
def make_microstructure(absorbing: bool):
    """Baseline OFI ~ ±100/min; nello sweep il bid si ricarica (OFI fortemente positivo)."""
    q, t = [], []
    base = pd.Timestamp(f"{DAY} 09:24:00", tz=TZ)
    for m in range(16):  # baseline 09:24-09:39
        for k in range(4):
            ts = base + pd.Timedelta(minutes=m, seconds=k * 15)
            q.append((ts, 100.40, 300 + 50 * k if m % 2 == 0 else 450 - 50 * k, 100.41, 300))
    start = pd.Timestamp(f"{DAY} 09:40:00", tz=TZ)
    for k in range(40):
        ts = start + pd.Timedelta(seconds=k)
        size = 300 + (k * 400 if absorbing else 0)  # ricarica passiva sul bid
        q.append((ts, 100.30, size, 100.31, 300))
    quotes = pd.DataFrame(q, columns=["ts", "bid_price", "bid_size", "ask_price", "ask_size"]).set_index("ts")
    # Trade: vendite aggressive fino all'estremo 100.25, poi acquisti (CVD non fa nuovi minimi).
    for k in range(10):
        t.append((start + pd.Timedelta(seconds=k), 100.30 - k * 0.005, 100))
    for k in range(30):
        t.append((start + pd.Timedelta(seconds=10 + k), 100.31, 200 if k % 3 == 0 else 50))
    trades = pd.DataFrame(t, columns=["ts", "price", "size"]).set_index("ts")
    return quotes, trades, start


def test_ofi_formula_bid_replenishment_is_positive():
    idx = pd.date_range(f"{DAY} 09:40", periods=3, freq="1s", tz=TZ)
    q = pd.DataFrame({"bid_price": [100.0, 100.0, 100.0], "bid_size": [100, 300, 600],
                      "ask_price": [100.01] * 3, "ask_size": [200] * 3}, index=idx)
    assert list(ofi_events(q)) == [200.0, 300.0]


def test_absorption_confirmed_vs_rejected():
    quotes, trades, start = make_microstructure(absorbing=True)
    res = check_absorption("long", quotes, trades, start, CFG["strategy"])
    assert res.ok, res.reason
    assert res.ofi_ratio >= CFG["strategy"]["ofi_imbalance_factor"] and res.cvd_divergence
    assert res.poc == pytest.approx(100.31)
    quotes, trades, start = make_microstructure(absorbing=False)
    assert not check_absorption("long", quotes, trades, start, CFG["strategy"]).ok


def test_point_of_control():
    tr = pd.DataFrame({"price": [10.0, 10.01, 10.01, 10.02], "size": [5, 10, 10, 1]})
    assert point_of_control(tr, 0.01) == pytest.approx(10.01)


# --------------------------------------------------------- classi del report
def test_consistency_optimizer():
    co = ConsistencyOptimizer(3000, 0.50)
    assert co.hard_daily_cap == 1500 and co.operational_daily_cap == pytest.approx(1350)
    assert co.evaluate_trading_state(1400, 0) is False
    assert co.evaluate_trading_state(1400, 5000) is True
    assert co.max_daily_profit(0) == pytest.approx(1350)
    assert co.max_daily_profit(5000) == pytest.approx(0.45 * 5000 / 0.55)
    # al cap calcolato il giorno è esattamente al limite operativo
    d = co.max_daily_profit(5000)
    assert d == pytest.approx((5000 + d) * 0.45)


def test_dynamic_risk_splitter():
    rs = DynamicRiskSplitter(50_000, 0.02)
    assert rs.usable_daily_budget == pytest.approx(850)
    assert rs.compute_position_size(0, 0.5, 3) == 566
    assert rs.compute_position_size(-425, 0.5, 3) == 283  # size dimezzata a metà budget
    assert rs.compute_position_size(-850, 0.5, 3) == 0    # lock-out


def test_dampener_and_timer():
    d = DrawdownDampenerAndTimer(100, 50, time(15, 50))
    now = datetime(2026, 10, 1, 10, 0)
    assert d.evaluate_tick_state(80, now) is False
    assert d.evaluate_tick_state(120, now) is False
    assert d.evaluate_tick_state(69, now) is True          # -51 dal picco 120
    assert DrawdownDampenerAndTimer(100, 50, time(15, 50)).evaluate_tick_state(0, datetime(2026, 10, 1, 15, 50))


def test_account_guard_modes():
    g = AccountGuard("eod_trailing_lock", 50_000, 2_000, 50_000, 50_000)
    assert g.floor() == 48_000
    g.end_of_day(51_000)
    assert g.floor() == 49_000
    g.end_of_day(53_000)
    assert g.floor() == 50_000                               # si blocca al valore iniziale
    a = AccountGuard("intraday_trailing", 50_000, 2_500, 50_000, 50_000)
    a.update(51_000)                                         # HWM con PnL non realizzato
    assert a.update(48_400) is True


# --------------------------------------------------------------- risk manager
def cand(extreme=99.5, close=100.0, atr=0.4):
    return SweepCandidate("SPY", "long", "ORL", 99.8, extreme, pd.Timestamp(f"{DAY} 09:40", tz=TZ),
                          close, atr, pd.Timestamp(f"{DAY} 09:41", tz=TZ))


ABS = Absorption(True, "ok", 4.0, 100.0, True, 99.9)


def test_plan_limit_on_poc_stop_beyond_sweep():
    rm = RiskManager(CFG, starting_equity=100_000)
    plan = rm.plan(cand(), ABS, 100_000, DayState(DAY, 100_000))
    assert isinstance(plan, TradePlan)
    assert plan.entry_limit == 99.9                      # POC sotto l'ultimo prezzo
    assert plan.stop == pytest.approx(99.46)             # oltre la coda: 99.5 - 0.1*ATR
    assert plan.qty == 500                               # cap nozionale (splitter darebbe 1287)
    assert plan.take_profit == pytest.approx(101.22) and not plan.tp_clipped_by_consistency


def test_plan_profit_clipping_and_rejection():
    rm = RiskManager(CFG, starting_equity=100_000)       # cap = 6000 * 0.40 * 0.90 = 2160
    plan = rm.plan(cand(), ABS, 100_000, DayState(DAY, 100_000, realized_pnl=1800))
    assert isinstance(plan, TradePlan) and plan.tp_clipped_by_consistency
    assert plan.qty * (plan.take_profit - plan.entry_limit) <= 360 + plan.qty * 0.01
    assert isinstance(rm.plan(cand(), ABS, 100_000, DayState(DAY, 100_000, realized_pnl=2000)), Rejection)
    assert isinstance(rm.plan(cand(), ABS, 100_000, DayState(DAY, 100_000, realized_pnl=2200)), Rejection)


def test_friction_filter():
    rm = RiskManager(CFG, starting_equity=100_000)
    tiny = cand(extreme=99.98, close=100.0, atr=0.01)
    res = rm.plan(tiny, Absorption(True, "ok", 4, 1, True, 99.99), 100_000, DayState(DAY, 100_000))
    assert isinstance(res, Rejection) and "friction" in res.reason


def test_overrides_are_whitelisted_and_clamped():
    cfg = apply_overrides(CFG, {"risk.hard_take_profit_r": 99, "risk.daily_loss_limit_pct": 50})
    assert cfg["risk"]["hard_take_profit_r"] == 4.0
    assert cfg["risk"]["daily_loss_limit_pct"] == CFG["risk"]["daily_loss_limit_pct"]


# ---------------------------------------------------------------------- coach
def report(*adjs):
    return ai_coach.CoachReport(
        diagnosis="", consistency_assessment="", no_change_reason="", risk_flags=[], confidence="medium",
        adjustments=[ai_coach.Adjustment(parameter=p, current_value=0, new_value=v, rationale="",
                                         evidence_trade_ids=ev, expected_effect="") for p, v, ev in adjs],
    )


def test_coach_guardrails_with_unvalidated_edge():
    r = report(("risk.hard_take_profit_r", 2.0, ["a"]),     # giù = safer: ok, limitato a -20%
               ("risk.atr_stop_mult", 0.8, ["b"]),          # giù ma safer=up: scartato (DSR basso)
               ("strategy.ofi_imbalance_factor", 4.0, []))  # senza evidenze: scartato
    approved, _ = ai_coach.validate_adjustments(CFG, r, edge_ok=False)
    assert approved == {"risk.hard_take_profit_r": 2.4}
    approved, _ = ai_coach.validate_adjustments(CFG, report(("risk.atr_stop_mult", 0.8, ["b"])), edge_ok=True)
    assert approved == {"risk.atr_stop_mult": 0.8}


def test_trial_rollback_on_worse_expectancy():
    trades = [{"r_multiple": 0.5, "pnl": 50, "day": f"d{i}"} for i in range(10)]
    trades += [{"r_multiple": -0.5, "pnl": -50, "day": f"e{i}"} for i in range(10)]
    state = {"active": {"risk.hard_take_profit_r": 2.4}, "history": [{
        "status": "trial", "closed_trades_at_change": 10, "previous_active": {},
        "baseline_stats": {"expectancy_r": 0.5, "best_day_share": 0.1}, "consistency_threshold": 0.4,
        "changes": {"risk.hard_take_profit_r": 2.4},
    }]}
    msg = ai_coach.evaluate_trial(state, trades, copy.deepcopy(CFG["coach"]))
    assert msg.startswith("ROLLBACK") and state["active"] == {}


def test_dsr_penalises_multiple_testing():
    rs = [1.0, -0.5, 0.8, -0.4, 1.2, -0.6, 0.9, 0.1, -0.5, 1.1] * 4
    assert deflated_sharpe_ratio(rs, 1) > deflated_sharpe_ratio(rs, 46) > deflated_sharpe_ratio(rs, 1000)


def test_stats_best_day_share():
    s = compute_stats([{"pnl": 100, "day": "a", "r_multiple": 1}, {"pnl": 300, "day": "b", "r_multiple": 2}])
    assert s.best_day_share == 0.75 and s.n == 2
