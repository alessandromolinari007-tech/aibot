import copy
from datetime import date

import pandas as pd
import pytest

import ai_coach
from bot.config import apply_overrides, load_config
from bot.journal import compute_stats
from bot.risk import DayState, Rejection, RiskManager, TradePlan
from bot.strategy import Signal, compute_levels, detect_signal

CFG = load_config(with_overrides=False)
TZ = "America/New_York"


def make_bars(day_prev="2026-09-30", day="2026-10-01"):
    """Giorno precedente piatto 100-101, pre-market 100.2-100.8, poi sweep di PDL a 10:00."""
    rows = []
    for ts in pd.date_range(f"{day_prev} 09:30", f"{day_prev} 15:55", freq="5min", tz=TZ):
        rows.append((ts, 100.5, 101.0, 100.0, 100.5, 1000))
    for ts in pd.date_range(f"{day} 04:00", f"{day} 09:25", freq="5min", tz=TZ):
        rows.append((ts, 100.5, 100.8, 100.2, 100.5, 300))
    for ts in pd.date_range(f"{day} 09:30", f"{day} 09:55", freq="5min", tz=TZ):
        rows.append((ts, 100.5, 100.8, 100.2, 100.4, 1000))
    t = pd.Timestamp(f"{day} 10:00", tz=TZ)
    rows.append((t, 100.4, 100.45, 99.9, 99.95, 3000))                           # sweep sotto PDL=100
    rows.append((t + pd.Timedelta("5min"), 99.95, 100.6, 99.93, 100.55, 2000))  # reclaim + displacement
    df = pd.DataFrame(rows, columns=["ts", "open", "high", "low", "close", "volume"]).set_index("ts")
    return df


def test_levels_and_long_sweep_signal():
    df = make_bars()
    levels = compute_levels(df, date(2026, 10, 1), CFG["session"])
    assert levels["PDL"] == 100.0 and levels["PDH"] == 101.0
    assert levels["PML"] == 100.2 and levels["PMH"] == 100.8
    sig = detect_signal("SPY", df, levels, CFG["strategy"], CFG["session"])
    assert sig is not None and sig.side == "long" and sig.level_name in {"PDL", "PML"}
    assert sig.sweep_extreme == pytest.approx(99.9)


def test_no_signal_when_level_already_used_and_outside_killzone():
    df = make_bars()
    levels = compute_levels(df, date(2026, 10, 1), CFG["session"])
    assert detect_signal("SPY", df, levels, CFG["strategy"], CFG["session"], {"PDL", "PML"}) is None
    late = df.copy()
    late.index = late.index + pd.Timedelta("3h")
    assert detect_signal("SPY", late, levels, CFG["strategy"], CFG["session"]) is None


def sig(entry=100.0, extreme=99.5, atr=0.4, side="long"):
    return Signal("SPY", side, "PDL", 99.8, extreme, entry, atr, pd.Timestamp("2026-10-01 10:00", tz=TZ))


def test_plan_sizing_and_atr_stop():
    rm = RiskManager(CFG, starting_equity=100_000)
    day = DayState("2026-10-01", 100_000)
    plan = rm.plan(sig(), 100_000, day)
    assert isinstance(plan, TradePlan)
    # stop: max(1.5*ATR=0.6, strutturale 100-(99.5-0.04)=0.54) = 0.6
    assert plan.stop == pytest.approx(99.4)
    assert plan.qty * plan.risk_per_share <= 500 + 1e-6  # 0.5% di 100k
    assert plan.take_profit == pytest.approx(101.2)  # 2R


def test_consistency_cap_clips_take_profit():
    rm = RiskManager(CFG, starting_equity=100_000)
    # cap = min(1.5% * 100k = 1500, 0.30 * 8000 * 0.9 = 2160) = 1500
    day = DayState("2026-10-01", 100_000, realized_pnl=1000)
    assert rm.daily_profit_cap(day) == pytest.approx(1500)
    plan = rm.plan(sig(), 100_000, day)
    assert isinstance(plan, TradePlan) and plan.tp_clipped_by_consistency
    assert plan.qty * abs(plan.take_profit - plan.entry_ref) <= 500 + plan.qty * 0.01


def test_consistency_rejects_when_room_too_small():
    rm = RiskManager(CFG, starting_equity=100_000)
    day = DayState("2026-10-01", 100_000, realized_pnl=1300)
    res = rm.plan(sig(), 100_000, day)
    assert isinstance(res, Rejection)


def test_daily_limits_block():
    rm = RiskManager(CFG, starting_equity=100_000)
    assert rm.can_trade(DayState("d", 100_000, realized_pnl=-2000))
    assert rm.can_trade(DayState("d", 100_000, realized_pnl=1500))
    assert rm.can_trade(DayState("d", 100_000, consecutive_losses=2))
    assert rm.can_trade(DayState("d", 100_000)) is None


def test_trailing_never_loosens():
    rm = RiskManager(CFG, starting_equity=100_000)
    assert rm.trail_stop("long", 100, 99.4, 0.6, 100.3, 0.4) == 99.4      # < 1R: invariato
    assert rm.trail_stop("long", 100, 99.4, 0.6, 100.7, 0.4) == 100.3     # trailing > breakeven
    assert rm.trail_stop("long", 100, 100.3, 0.6, 100.65, 0.4) == 100.3   # non si allarga
    assert rm.trail_stop("short", 100, 100.6, 0.6, 99.3, 0.4) == 99.7


def test_overrides_are_whitelisted_and_clamped():
    cfg = apply_overrides(CFG, {"risk.take_profit_r": 99, "risk.daily_loss_limit_pct": 50})
    assert cfg["risk"]["take_profit_r"] == 4.0
    assert cfg["risk"]["daily_loss_limit_pct"] == CFG["risk"]["daily_loss_limit_pct"]


def test_coach_guardrails():
    report = ai_coach.CoachReport(
        diagnosis="", consistency_assessment="", no_change_reason="", risk_flags=[], confidence="medium",
        adjustments=[
            ai_coach.Adjustment(parameter="risk.take_profit_r", current_value=2, new_value=1.0,
                                rationale="", evidence_trade_ids=["a"], expected_effect=""),
            ai_coach.Adjustment(parameter="risk.atr_stop_mult", current_value=1.5, new_value=1.6,
                                rationale="", evidence_trade_ids=[], expected_effect=""),
        ],
    )
    approved, _ = ai_coach.validate_adjustments(CFG, report)
    assert approved == {"risk.take_profit_r": 1.6}  # max -20% per iterazione; senza evidenze scartato


def test_trial_rollback_on_worse_expectancy():
    coach_cfg = copy.deepcopy(CFG["coach"])
    trades = [{"r_multiple": 0.5, "pnl": 50, "day": f"d{i}"} for i in range(10)]
    trades += [{"r_multiple": -0.5, "pnl": -50, "day": f"e{i}"} for i in range(10)]
    state = {
        "active": {"risk.take_profit_r": 1.6},
        "history": [{
            "status": "trial", "closed_trades_at_change": 10, "previous_active": {},
            "baseline_stats": {"expectancy_r": 0.5, "best_day_share": 0.1}, "max_day_share": 0.3,
            "changes": {"risk.take_profit_r": 1.6},
        }],
    }
    msg = ai_coach.evaluate_trial(state, trades, coach_cfg)
    assert msg.startswith("ROLLBACK") and state["active"] == {}


def test_stats_best_day_share():
    s = compute_stats([{"pnl": 100, "day": "a", "r_multiple": 1}, {"pnl": 300, "day": "b", "r_multiple": 2}])
    assert s.best_day_share == 0.75 and s.n == 2
