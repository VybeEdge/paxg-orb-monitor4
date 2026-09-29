"""
STATELESS PAXGUSD ORB monitor, designed to run as a GitHub Actions job on a
schedule (see .github/workflows/live-monitor.yml). No API key needed - all
data used here is Delta Exchange's public market data.

*** Settings below (STOP_MULT_1H / TRAIL_MULT / ACTIVATION_R) are walk-forward *
*** validated directly against THIS file's process_bar(), fee-inclusive, on   *
*** PAXGUSD's own 7+ month history - see the settings block comment below for *
*** the numbers and methodology.                                             *

WHY "STATELESS": every GitHub Actions run starts a brand-new, empty machine
with no memory of the last run. So this script:
  1. loads state.json (committed in the repo) to see where it left off
  2. re-fetches every 1-minute PAXGUSD candle since the last bar it processed,
     from Delta's server (the authoritative, unchanging record) - all day,
     every day, not just during the trading session
  3. replays only the candles it hasn't processed yet through the EXACT same
     bar-by-bar logic as the original backtest/live-monitor script
  4. saves its new position back to state.json
This makes the result identical to what a continuously-running version would
have computed for the same closed candles - see the chat explanation for why.
The one real difference is notification timing (checked every few minutes
here, not instantly), not the correctness of what gets logged.

Fetching runs around the clock (see the workflow's cron), and so does the
strategy itself - PAXGUSD trades 24/7, so process_bar() opens a fresh
SESSION_HOURS-long opening range and watches for signals continuously, with
no NY-session restriction and no daily flatten.

This file doubles as the backtest engine: run_backtest.py imports it, points
ATR_FN and the *_LOG file constants at a local OHLC lookup and backtest_*.csv
filenames, and replays a full historical CSV bar-by-bar through the exact
same process_bar() used live - so the backtest and the live monitor can never
silently drift apart into two different implementations of "the strategy."

FILES this writes/updates in the repo (the workflow commits them every run):
  state.json       - internal bookkeeping, not meant to be read by a person
  live_log.txt     - human-readable running narrative (open this to just read)
  alerts_log.csv   - every early-warning alert: FIRED, then RESOLVED with
                     whether a real breakout followed
  trades_log.csv   - every paper trade, with entry/exit/result/R, AND the
                     dollar effect on three simulated account sizes
  price_and_equity.csv - one row per processed 1-min bar: PAXGUSD close +
                     running % return (same for every balance tier, since
                     they're all risking the same 1% - only the dollar
                     amounts differ) + each tier's dollar balance. This is
                     what a dashboard chart should plot.
  SUMMARY.md       - always-current headline stats, rewritten every run

All timestamps written to the CSVs are UTC (ISO 8601, e.g. 2026-09-26T12:34:00Z)
- unambiguous, so any viewer (dashboard, spreadsheet) can convert to whatever
local time zone it needs (New York market time, IST, etc.) itself.

INDIAN INCOME TAX - deliberately NOT deducted from the simulated balances
below, and here's why, since it was asked about directly: PAXGUSD on Delta
is a perpetual FUTURES contract, not a spot crypto purchase. Per Delta
Exchange's own published guidance ("Is there 30% VDA tax applicable on
trading profits?"), the flat 30% VDA tax + 1% TDS regime (Section 115BBH /
194S) applies only to buying/selling the underlying virtual digital asset
itself, NOT to F&O/derivatives trading - futures/options profits are
instead taxed as regular income at the trader's own income-tax SLAB rate.
That's structurally different from every other cost modeled in this file:
it's not withheld per trade, it's an annual liability settled separately
(via ITR) out of whatever bracket the trader's total income falls into
(0/5/10/15/20/25/30%+surcharge+4% cess) - so it doesn't compound through
the strategy the way the exchange's cut does, and this script has no way
to know an individual trader's slab or other income. The CAGR/balance
figures throughout this repo are therefore PRE-TAX: real take-home
compounding will be lower once tax is paid on realized profits, and how
much lower depends entirely on the trader's own tax situation - consult a
CA for that number, this isn't tax advice.
"""

import csv
import json
import os
import time
import datetime as dt
from collections import deque

import requests

# ============================== settings ===================================
# v4 (multi-concurrent-trade redesign) - see chat for the full research
# writeup. Entry detection is UNCHANGED from the v3 design: a fresh opening
# range every SESSION_HOURS UTC hours (24/7, no NY session), momentum-
# confirmed breakout, 1h EMA-100 trend filter, 1h-ATR-based stop, chandelier
# trailing exit. What's new in v4 is MAX_CONCURRENT_TRADES > 1 - the user
# wanted more trades/day than v3's ~0.3/day; here's what was actually tried:
#   - Locking in small gains fast (tight TRAIL_MULT/ACTIVATION_R) to raise
#     both frequency and win rate: win rate DID rise to 65%+, but CAGR
#     collapsed to -90%+. Delta's ~0.118% round-trip fee is a near-fixed tax
#     per trade; a small locked-in win doesn't clear it, and losses are still
#     a full -1R, so this made every trade a net loser on average.
#   - A hard cap on how long a trade can stay open (MAX_HOLD_HOURS), to free
#     up the single trade slot faster: it DID raise frequency, but forced
#     exits before a trade could resolve destroyed the very edge that pays
#     for the fee - even worse than the above (results.TIME-exits dominated,
#     averaging close to breakeven R before fees, deeply negative after).
#   - A fixed profit target instead of trailing (TP_R), for a steadier win
#     rate: best case found was roughly breakeven to a few % CAGR - nowhere
#     near the target, and this signal's real edge lives in occasionally
#     catching a large trend move, which a fixed nearby target cuts off.
#   - What actually works: the entry logic already finds ~5-6 independent
#     breakout episodes a day (measured directly, ignoring any trade-slot
#     limit) - they were being thrown away by only ever having ONE trade
#     open at a time while a winner trails for hours or days. Letting
#     MAX_CONCURRENT_TRADES trades run at once, each with v3's already-
#     proven trailing exit and its own independent 1% risk, is what actually
#     buys frequency without wrecking the edge. The cost: concurrent trades
#     on the same instrument from the same signal are correlated, not
#     diversified, so drawdown grows with concurrency too - MAX_CONCURRENT_
#     TRADES=3 was chosen (over 4+) specifically because it's the highest
#     level that still validates robustly out-of-sample (see below), not
#     just on the full history.
# Walk-forward validated (70/30 train/test split) DIRECTLY against this
# file's own process_bar() (sweep_v3.py methodology, extended for MAX_
# CONCURRENT_TRADES), with real trading costs ACTUALLY deducted from the
# tier balance at every trade close - see the CUT_PCT_* settings below for
# the current cost model (rev'd once already, see that comment for why):
#   full history: 146 trades (0.67/day), 34.2% win rate, +29.1% CAGR, maxDD -26.6%
#   train:        102 trades (0.67/day), 33.3% win rate, +24.6% CAGR, maxDD -20.9%
#   test:          44 trades (0.67/day), 36.4% win rate, +39.7% CAGR, maxDD -16.5%
# (These replace an earlier +61.5%/+100.8% train/test estimate that used a
# flat 0.118%-of-notional fee; re-priced once a proper holding-time-tiered
# cost was modeled - see CUT_PCT_* below - since most of this design's
# winners take 8+ hours to develop and now pay the top tier of that cost.
# A fresh 540-combo re-sweep of STOP_MULT_1H/TRAIL_MULT/ACTIVATION_R/
# MAX_CONCURRENT_TRADES against the corrected cost model found these same
# settings were STILL the best of everything tried - no free lunch hiding
# in a different corner of the grid.) Both segments still clear the user's
# original 25-30% target (train sits right at the edge of it, test comfortably
# above), and the test segment holds up at least as well as train, not just
# a fit that happened to work on the tuning window. Trade frequency (0.67/
# day, roughly 2x v3) is well short of the 3-6/day originally asked for;
# going further (MAX_CONCURRENT_TRADES=4-5, or any of the exit changes
# above) either broke out-of-sample validation or crushed the win rate/
# return past what's survivable under real costs - see the chat writeup.
BASE_URL = "https://cdn.india.deltaex.org"
SYMBOL = "PAXGUSD"

SESSION_HOURS = 4          # a fresh opening-range "session" every 4 UTC hours (6/day)
RANGE_MINUTES = 15         # minutes at the start of each session used to set the range

STOP_MULT_1H = 3.5         # initial stop = this many multiples of 1h ATR14
TRAIL_MULT = 1.5           # once activated, trail this many 1h-ATR multiples behind the peak
ACTIVATION_R = 3.0         # trailing starts once the trade is this many R into profit
ALERT_ATR_FRAC = 0.20      # early-warning "approaching the range edge" zone (15m ATR based)
MOMENTUM_BARS = 3          # require this many consecutive rising/falling closes to confirm
MAX_HOLD_HOURS = None      # optional cap on how long one trade can stay open, forced exit at
                            # market if exceeded (frees a slot for the next signal). None = no
                            # cap - tried this to boost frequency and it backfired badly (see
                            # the settings-block comment): truncating a trade before it
                            # resolves destroys the very edge that pays for the fee on it.
TP_R = None                # optional FIXED target, in initial-risk multiples, e.g. 1.4 means
                            # "take profit at 1.4x the initial stop distance". None = pure
                            # trailing exit (this design). Setting this switches OFF trailing
                            # entirely for that trade (see process_bar) - tried this too (a
                            # fixed bracket for a higher win rate) and it couldn't clear real
                            # fees at any frequency worth having - see the settings comment.
MAX_CONCURRENT_TRADES = 3  # how many trades can be open at once - THIS is what actually buys
                            # more trades/day here, not a shorter hold or a tighter target
                            # (both tried, both failed - see below). Each concurrent trade
                            # still risks its own 1% of balance independently.

BALANCES = [100.0, 1000.0, 10000.0]   # the three simulated account sizes
RISK_PERCENT = 1.0                    # fixed % risk per trade, same as TEST_fixed.py

# POSITION SIZE vs MARGIN - answering "aren't you using the whole balance per
# trade, with up to 3 open at once?" Position NOTIONAL (qty * entry) here
# averages ~89% of balance per trade (measured across all 146 historical
# trades; median 78%, up to several hundred % on rare wide-stop outliers) -
# that number on its own does look like "the whole balance." But notional
# is the leveraged position's full VALUE, not the capital actually locked
# to open it - that's MARGIN = notional / leverage, and leverage is a
# setting chosen on Delta at trade time (PAXGUSD supports up to 100x), not
# something this script needs to size for. At 10x leverage, ~89% notional
# ->  ~8.9% of balance in margin per trade - already inside the 5-10%
# target - and worst case, all 3 concurrent slots filled at once, that's
# only ~27% of balance locked, not "the whole balance." Deliberately NOT
# implemented as a notional cap in the sizing math: capping notional
# directly (instead of just choosing sane leverage) would force qty down
# too, which shrinks the DOLLAR risk per trade far below RISK_PERCENT and
# guts the validated CAGR for no real safety benefit - the actual fix here
# is "use ~10-20x leverage on Delta," not "risk less than intended."

# Cost model: replaced the flat notional-fee estimate with the user's own
# reported real-world numbers (from actually trading on Delta), since those
# are ground truth from a real account rather than a support article, and
# per the user "it's not just brokerage... total cut including everything" -
# i.e. this is meant to already bundle trading fee + funding + slippage,
# not just the exchange's quoted taker fee. It scales with how long the
# trade is held open - three tiers, each one EDITABLE here directly:
CUT_PCT_UNDER_2H = 9.0    # held < 2 hours   -> reported range was ~8-10%, this uses the midpoint
CUT_PCT_2_TO_8H = 15.0    # held 2-8 hours   -> reported ~15%
CUT_PCT_OVER_8H = 25.0    # held 8+ hours    -> reported ~25% (the max end of the range)
# One thing had to be decided that wasn't specified: 8-25% OF WHAT. Taken as
# a % of NOTIONAL (like a normal exchange fee) these numbers would be absurd
# - 25% of the full leveraged position size, every trade, would make any
# strategy unviable outright. Taken instead as a % of the DOLLAR AMOUNT
# RISKED on the trade (risk_dollars = balance * RISK_PERCENT/100, i.e. the
# 1% pre-fee "stake"), the low end lines up almost exactly with what was
# already modeled from Delta's own published taker+GST fee for a short hold
# (~10.8% of risk$ at this strategy's typical stop width) - so that's the
# basis used below. This also makes the cost properly holding-time-
# dependent, which the old flat notional fee never was, matching the real
# mechanism (funding accrues the longer a position stays open).
CUT_TIERS_HOURS_PCT = [(2.0, CUT_PCT_UNDER_2H), (8.0, CUT_PCT_2_TO_8H), (float("inf"), CUT_PCT_OVER_8H)]


def cut_pct_for_hold(hold_hours):
    for threshold, pct in CUT_TIERS_HOURS_PCT:
        if hold_hours < threshold:
            return pct
    return CUT_TIERS_HOURS_PCT[-1][1]

STATE_FILE = "state.json"
LIVE_LOG = "live_log.txt"
ALERTS_LOG = "alerts_log.csv"
TRADES_LOG = "trades_log.csv"
PRICE_LOG = "price_and_equity.csv"
SUMMARY_FILE = "SUMMARY.md"

TRADES_HEADER = (
    ["entry_time_utc", "side", "entry", "sl", "tp", "exit_time_utc", "exit", "result", "R", "had_alert"]
    + [f"{int(b)}_before" for b in BALANCES]
    + [f"{int(b)}_gross_before" for b in BALANCES]  # the parallel no-cost-ever-charged balance track
    + [f"{int(b)}_gross_pnl" for b in BALANCES]  # GROSS pnl - this trade's R applied to gross_before
    + [f"{int(b)}_pnl" for b in BALANCES]      # NET pnl (cut already subtracted) - after minus before
    + [f"{int(b)}_after" for b in BALANCES]
    + [f"{int(b)}_gross_after" for b in BALANCES]
    + [f"{int(b)}_fee" for b in BALANCES]      # how much of that was the est. holding-time cut, for transparency
)
ALERTS_HEADER = ["event", "alert_id", "time_utc", "side", "price", "range_high", "range_low", "atr", "outcome"]
PRICE_HEADER = (["time_utc", "close", "pct_return"] + [f"{int(b)}_balance" for b in BALANCES]
                 + [f"{int(b)}_gross_balance" for b in BALANCES])


def utc_iso(ts):
    return dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ------------------------------ small helpers -------------------------------
def log_line(msg):
    ts = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    line = f"[{ts}] {msg}"
    print(line)
    with open(LIVE_LOG, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def ensure_csv(path, header):
    if not os.path.exists(path):
        with open(path, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(header)


def append_csv(path, header, row):
    ensure_csv(path, header)
    with open(path, "a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)


def fetch_candles(session, resolution, start, end):
    r = session.get(f"{BASE_URL}/v2/history/candles",
                     params=dict(symbol=SYMBOL, resolution=resolution, start=start, end=end),
                     timeout=20)
    r.raise_for_status()
    data = r.json()
    rows = data.get("result", []) if data.get("success") else []
    return sorted(rows, key=lambda r: r["time"])


def atr14_m15(session, before_ts):
    rows = fetch_candles(session, "15m", before_ts - 15 * 60 * 30, before_ts - 15 * 60)
    if len(rows) < 15:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i]["high"], rows[i]["low"], rows[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / 14


def _live_atr_fn(bar_time):
    return atr14_m15(SESSION, bar_time)


# process_bar() calls ATR_FN(bar_time), never atr14_m15 directly, so a backtest
# script can point this at a local, already-loaded OHLC lookup instead of a
# live network call, without touching a single line of the strategy logic.
ATR_FN = _live_atr_fn


def atr14_h1(session, before_ts):
    """Same ATR14-of-true-range math as atr14_m15, but on 1h candles - used
    to size the initial stop/trail distance (see STOP_MULT_1H/TRAIL_MULT).
    ATR_FN (15m) stays as-is, used only for the early-warning alert zone."""
    rows = fetch_candles(session, "1h", before_ts - 3600 * 30, before_ts)
    if len(rows) < 15:
        return None
    trs = []
    for i in range(1, len(rows)):
        h, l, pc = rows[i]["high"], rows[i]["low"], rows[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    return sum(trs[-14:]) / 14


def _live_atr1h_fn(bar_time):
    return atr14_h1(SESSION, bar_time)


# same dependency-injection pattern as ATR_FN/EMA_FN
ATR1H_FN = _live_atr1h_fn


# ------------------------------ trend filter ---------------------------------
# Backtesting (see chat) showed a raw breakout entry is NOT robust out-of-
# sample: it looks great on whichever slice of history it was tuned on and
# falls apart on the next one. Requiring the breakout to agree with a slower,
# higher-timeframe trend (1h EMA-100) was the one filter that held up in BOTH
# the train and the held-out test windows - see EMA_PERIOD below.
EMA_PERIOD = 100
EMA_LOOKBACK_HOURS = 24 * 30  # 30 days of 1h candles - plenty for a 100-period EMA to converge


def ema_of_closes(closes, period):
    if len(closes) < period:
        return None
    ema = closes[0]
    k = 2 / (period + 1)
    for px in closes[1:]:
        ema = px * k + ema * (1 - k)
    return ema


def ema100_h1(session, before_ts):
    rows = fetch_candles(session, "1h", before_ts - EMA_LOOKBACK_HOURS * 3600, before_ts)
    return ema_of_closes([r["close"] for r in rows], EMA_PERIOD)


def _live_ema_fn(bar_time):
    return ema100_h1(SESSION, bar_time)


# same dependency-injection pattern as ATR_FN, for the same reason
EMA_FN = _live_ema_fn


def utc_day_key(ts_utc):
    return dt.datetime.fromtimestamp(ts_utc, tz=dt.timezone.utc).date().isoformat()


def default_tier():
    # gross_balance compounds the SAME trades (same R per trade) as if the
    # holding-time cut were never charged - a parallel hypothetical, kept
    # alongside the real (net) balance so gross vs net can be compared at
    # the account level, not just per-trade.
    return dict(balance=None, peak=None, max_dd_pct=0.0, trades=0, wins=0, losses=0, fees=0.0,
                gross_balance=None)


def default_day_state(day_key):
    """Resets once per UTC calendar day - just the trade counter."""
    return dict(day=day_key, trades_today=0)


def default_session_state(session_key):
    """Resets every SESSION_HOURS (a fresh opening range each time), independent
    of the once-a-day trade counter above. NOTE: open_trades is NOT part of this -
    a position can run past a session boundary until it hits SL/TP, so it lives
    at the top level of state instead (see load_state/process_bar)."""
    return dict(session=session_key, range_high=None, range_low=None, day_atr=None, session_ema=None,
                range_ok=False, alerted_up=False, alerted_dn=False, closes_before=[])


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE) as f:
            return json.load(f)
    s = dict(last_bar_time=None, open_trades=[],
              tiers={str(int(b)): {**default_tier(), "balance": b, "peak": b, "gross_balance": b} for b in BALANCES},
              alerts_total=0, alerts_followed=0, alerts_not_followed=0,
              trades_total=0, trades_wins=0, trades_losses=0, sum_R=0.0,
              trades_with_alert=0, wins_with_alert=0, trades_without_alert=0, wins_without_alert=0)
    s.update(default_day_state(None))
    s.update(default_session_state(None))
    return s


def save_state(s):
    with open(STATE_FILE, "w") as f:
        json.dump(s, f, indent=2)


# ------------------------------- main engine --------------------------------
def close_trade(state, ot, result, exit_px, exit_iso, exit_ts):
    # R is always relative to the INITIAL risk distance fixed at entry, never
    # the current (possibly-ratcheted) trailing stop - so a trade that trails
    # up to lock in +1.4R and then gets stopped there still correctly reads
    # as a +1.4R win, not "distance to the moved stop."
    risk = ot["stop_dist"]
    hold_hours = max(0.0, (exit_ts - ot["entry_ts"]) / 3600.0)
    cut_pct = cut_pct_for_hold(hold_hours)
    R = ((exit_px - ot["entry"]) / risk if ot["side"] == 1 else (ot["entry"] - exit_px) / risk)
    state["trades_total"] += 1
    state["sum_R"] += R
    if R > 0:
        state["trades_wins"] += 1
    else:
        state["trades_losses"] += 1
    if ot["had_alert"]:
        state["trades_with_alert"] += 1
        if R > 0:
            state["wins_with_alert"] += 1
    else:
        state["trades_without_alert"] += 1
        if R > 0:
            state["wins_without_alert"] += 1

    befores, gross_befores, gross_pnls, pnls, afters, gross_afters, fees = [], [], [], [], [], [], []
    for b in BALANCES:
        key = str(int(b))
        tier = state["tiers"][key]
        before = tier["balance"]
        risk_dollars = before * (RISK_PERCENT / 100.0)
        pnl_gross_actual = R * risk_dollars
        # fee = the user's own reported "cut" %, tiered by how long this
        # trade was actually held, applied to the dollar amount risked (see
        # the settings-block comment for why that basis was chosen). Charged
        # on every close, win or lose, same as a real trading cost would be.
        fee = risk_dollars * (cut_pct / 100.0)
        pnl_net = pnl_gross_actual - fee
        after = max(0.0, before + pnl_net)
        tier["balance"] = after
        tier["fees"] += fee
        # parallel hypothetical: same R, compounding on its OWN gross balance
        # (1% of the gross track, not the net one) as if no cut were ever
        # charged - logged per-trade too (gross_before/gross_pnl/gross_after)
        # so the gross figures are self-consistent (gross_before + gross_pnl
        # = gross_after) rather than mixing a gross P/L onto the net balance.
        gross_before = tier["gross_balance"] if tier["gross_balance"] is not None else before
        gross_risk_dollars = gross_before * (RISK_PERCENT / 100.0)
        pnl_gross = R * gross_risk_dollars
        gross_after = max(0.0, gross_before + pnl_gross)
        tier["gross_balance"] = gross_after
        tier["peak"] = max(tier["peak"], after)
        dd = (after - tier["peak"]) / tier["peak"] * 100.0 if tier["peak"] > 0 else 0.0
        tier["max_dd_pct"] = min(tier["max_dd_pct"], dd)
        tier["trades"] += 1
        if R > 0:
            tier["wins"] += 1
        else:
            tier["losses"] += 1
        befores.append(round(before, 2))
        gross_befores.append(round(gross_before, 2))
        gross_pnls.append(round(pnl_gross, 2))
        pnls.append(round(pnl_net, 2))
        afters.append(round(after, 2))
        gross_afters.append(round(gross_after, 2))
        fees.append(round(fee, 4))

    log_line(f"PAPER TRADE CLOSED  {('BUY' if ot['side']==1 else 'SELL')} "
             f"entry {ot['entry']:.2f} -> exit {exit_px:.2f}  result={result}  R={R:.2f}  "
             f"(had_alert={ot['had_alert']}, held {hold_hours:.1f}h -> {cut_pct:.0f}% cut, "
             f"est. fee ${fees[0]:.4f} on the $100 tier)")
    # "sl" column = the ORIGINAL (never-moved) stop. "tp" is left blank -
    # there is no fixed target any more, the trailing stop above is where a
    # profitable trade actually exits. "gross_pnl" is BEFORE the cut; "pnl"/
    # "after" are NET of it (see "fee" columns) - both are logged so the
    # dashboard can show either, or both, per trade.
    append_csv(TRADES_LOG, TRADES_HEADER,
               [ot["entry_time"], "BUY" if ot["side"] == 1 else "SELL", ot["entry"], ot["init_stop"],
                "", exit_iso, exit_px, result, round(R, 3), ot["had_alert"]]
               + befores + gross_befores + gross_pnls + pnls + afters + gross_afters + fees)


def _finalize_unresolved_alerts(state, as_of_iso, last_price):
    """Called right before a session resets: any alert that FIRED this session
    but never saw a real breakout gets logged RESOLVED/NOT_FOLLOWED, so the
    dashboard's indicator-accuracy stat doesn't leave it hanging forever."""
    for flag_key, side_key, label in (("alerted_up", "UP", "BUY"), ("alerted_dn", "DN", "SELL")):
        if state.get(flag_key):
            aid = f"{state['session']}-{side_key}"
            state["alerts_not_followed"] += 1
            append_csv(ALERTS_LOG, ALERTS_HEADER,
                       ["RESOLVED", aid, as_of_iso, label, last_price,
                        state["range_high"], state["range_low"],
                        state["day_atr"], "NOT_FOLLOWED"])


def _manage_open_trade(state, ot, ts, h, l, c):
    """Update one open trade against this bar and close it if its exit
    condition is met. Returns True if it closed (caller removes it from
    state["open_trades"]). Two mutually exclusive exit styles, picked by
    whether TP_R is set:
      TP_R is None  -> trailing-only (the original design): no fixed target,
                       a chandelier stop that only starts ratcheting once
                       the trade is ACTIVATION_R risk-multiples into profit,
                       only ever tightens. Built for a small number of big,
                       left-to-run winners.
      TP_R is a number -> fixed bracket: stop stays at its initial ATR
                       distance (no trailing) and a fixed target sits at
                       TP_R * that same distance. Whichever is touched first
                       closes the trade. Built for a higher win rate at
                       higher frequency - see the settings-block comment.
    Either way, MAX_HOLD_HOURS (if set) force-exits at the current close if
    the trade has been open too long without resolving."""
    if TP_R is not None:
        if ot["side"] == 1:
            hit_sl, hit_tp = l <= ot["stop"], h >= ot["tp"]
        else:
            hit_sl, hit_tp = h >= ot["stop"], l <= ot["tp"]
        # if a single bar's range spans BOTH levels, assume the stop was
        # touched first (the conservative assumption - never assume the
        # more favorable fill when both are only known to have happened
        # somewhere within the same 1-minute bar)
        hit = hit_sl or hit_tp
        exit_px = ot["stop"] if hit_sl else ot["tp"]
    else:
        if ot["side"] == 1:
            if h > ot["mfe"]:
                ot["mfe"] = h
            if ot["mfe"] >= ot["entry"] + ACTIVATION_R * ot["stop_dist"]:
                new_stop = ot["mfe"] - TRAIL_MULT * ot["atr1h"]
                if new_stop > ot["stop"]:
                    ot["stop"] = new_stop
            hit = l <= ot["stop"]
            exit_px = ot["stop"]
        else:
            if l < ot["mfe"]:
                ot["mfe"] = l
            if ot["mfe"] <= ot["entry"] - ACTIVATION_R * ot["stop_dist"]:
                new_stop = ot["mfe"] + TRAIL_MULT * ot["atr1h"]
                if new_stop < ot["stop"]:
                    ot["stop"] = new_stop
            hit = h >= ot["stop"]
            exit_px = ot["stop"]
    timed_out = (MAX_HOLD_HOURS is not None and not hit
                 and (ts - ot["entry_ts"]) >= MAX_HOLD_HOURS * 3600)
    if timed_out:
        hit = True
        exit_px = c  # force-exit at this bar's close, not the stop
    if hit:
        if timed_out:
            result = "TIME"
        elif exit_px == ot["init_stop"]:
            result = "SL"  # stop never moved (fixed-bracket mode: it never does) - a straight loss
        else:
            result = "TP" if TP_R is not None else "TRAIL"  # target hit, or a trailing gain locked in
        close_trade(state, ot, result, exit_px, utc_iso(ts), ts)
        return True
    return False


def process_bar(state, bar):
    ts = bar["time"]
    day_key = utc_day_key(ts)
    if state.get("day") != day_key:
        state.update(default_day_state(day_key))

    session_key = ts // (SESSION_HOURS * 3600)
    o, h, l, c = bar["open"], bar["high"], bar["low"], bar["close"]

    # --- manage every open paper trade first (each can run past a session
    # boundary - there's no NY-style "flatten before close": PAXGUSD trades
    # 24/7). Up to MAX_CONCURRENT_TRADES can be open at once, each managed
    # completely independently (its own stop/trail/target/hold-timer, its
    # own 1% risk) - see _manage_open_trade() above for the exit logic. ---
    state["open_trades"] = [ot for ot in state["open_trades"]
                             if not _manage_open_trade(state, ot, ts, h, l, c)]

    if session_key != state.get("session"):
        if state.get("session") is not None:
            _finalize_unresolved_alerts(state, utc_iso(ts), c)
        state.update(default_session_state(session_key))

    session_start = session_key * SESSION_HOURS * 3600
    secs_into_session = ts - session_start

    # --- opening range build (first RANGE_MINUTES of each session block) ---
    # NOTE: falls through to the price/equity row at the bottom either way -
    # only the alert/signal detection below is skipped during this window.
    in_range_window = secs_into_session < RANGE_MINUTES * 60
    if in_range_window:
        state["range_high"] = h if state["range_high"] is None else max(state["range_high"], h)
        state["range_low"] = l if state["range_low"] is None else min(state["range_low"], l)
    else:
        if not state["range_ok"] and state["range_high"] is not None and state["day_atr"] is None:
            a = ATR_FN(ts)
            if a:
                state["day_atr"] = a
                state["session_ema"] = EMA_FN(ts)  # computed once per session, held for its duration
                # No range-width-vs-ATR filter any more (validated without one -
                # see research_v4.py); every session's range is watched once set.
                state["range_ok"] = True
                log_line(f"Session {session_key} range set: {state['range_low']:.2f}-{state['range_high']:.2f} "
                         f"(ATR15 {a}, EMA100(1h) {state['session_ema']}) -> active")
            # else: ATR not available yet (e.g. early warm-up period) - leave
            # day_atr as None so this retries again next bar in this session

        closes_before = deque(state["closes_before"], maxlen=MOMENTUM_BARS)

        # --- alerts + real signal --- (no per-day trade cap any more - see
        # settings block; "no open trade already" is the only gate, matching
        # what was actually validated - see chat writeup)
        if state["range_ok"] and len(state["open_trades"]) < MAX_CONCURRENT_TRADES:
            rh, rl, a = state["range_high"], state["range_low"], state["day_atr"]
            alert_zone = ALERT_ATR_FRAC * a

            if len(closes_before) == MOMENTUM_BARS:
                prior = list(closes_before)
                rising = all(prior[k] > prior[k - 1] for k in range(1, len(prior)))
                falling = all(prior[k] < prior[k - 1] for k in range(1, len(prior)))

                if not state["alerted_up"] and c < rh and (rh - h) <= alert_zone and rising:
                    state["alerted_up"] = True
                    aid = f"{session_key}-UP"
                    log_line(f"\U0001F514 ALERT FIRED - WATCH BUY  approaching {rh:.2f}  [alert_id={aid}]")
                    state["alerts_total"] += 1
                    append_csv(ALERTS_LOG, ALERTS_HEADER,
                               ["FIRED", aid, utc_iso(ts), "BUY", c, rh, rl, a, ""])

                if not state["alerted_dn"] and c > rl and (l - rl) <= alert_zone and falling:
                    state["alerted_dn"] = True
                    aid = f"{session_key}-DN"
                    log_line(f"\U0001F514 ALERT FIRED - WATCH SELL  approaching {rl:.2f}  [alert_id={aid}]")
                    state["alerts_total"] += 1
                    append_csv(ALERTS_LOG, ALERTS_HEADER,
                               ["FIRED", aid, utc_iso(ts), "SELL", c, rh, rl, a, ""])

            # momentum-confirmed breakout: require MOMENTUM_BARS consecutive
            # closes in the breakout's direction, not just a single spike
            mom_up_ok = len(closes_before) == MOMENTUM_BARS and all(
                closes_before[k] > closes_before[k - 1] for k in range(1, len(closes_before)))
            mom_dn_ok = len(closes_before) == MOMENTUM_BARS and all(
                closes_before[k] < closes_before[k - 1] for k in range(1, len(closes_before)))

            # trend filter: only take the breakout if it agrees with the slower
            # 1h EMA-100 - this is what separated "works out-of-sample" from
            # "looked great on the tuning window" in the chat's validation
            ema = state["session_ema"]
            trend_up_ok = ema is None or c > ema
            trend_dn_ok = ema is None or c < ema

            side = 1 if (c > rh and mom_up_ok and trend_up_ok) else (
                -1 if (c < rl and mom_dn_ok and trend_dn_ok) else 0)
            if side != 0:
                had_alert = state["alerted_up"] if side == 1 else state["alerted_dn"]
                aid = f"{session_key}-{'UP' if side == 1 else 'DN'}"
                if had_alert:
                    state["alerts_followed"] += 1
                    append_csv(ALERTS_LOG, ALERTS_HEADER,
                               ["RESOLVED", aid, utc_iso(ts), "BUY" if side == 1 else "SELL",
                                c, rh, rl, a, "BREAKOUT_FOLLOWED"])
                    # this alert is now settled - don't finalize it again as
                    # NOT_FOLLOWED at session end, and let the same side re-arm
                    state["alerted_up" if side == 1 else "alerted_dn"] = False
                atr1h = ATR1H_FN(ts)
                if atr1h and atr1h > 0:
                    stop_dist = STOP_MULT_1H * atr1h
                    init_stop = c - stop_dist if side == 1 else c + stop_dist
                    tp = None
                    if TP_R is not None:
                        tp = c + TP_R * stop_dist if side == 1 else c - TP_R * stop_dist
                    log_line(f">>> {'BUY' if side==1 else 'SELL'} SIGNAL  entry~{c:.2f}  initial stop {init_stop:.2f}  "
                             f"(1h ATR {atr1h:.2f}, {'target '+format(tp,'.2f') if tp is not None else 'trailing - no fixed target'}, "
                             f"had_alert={had_alert}, trade #{state['trades_today']+1} today)")
                    state["trades_today"] += 1
                    state["open_trades"].append(dict(side=side, entry=c, init_stop=init_stop, stop=init_stop,
                                                       stop_dist=stop_dist, atr1h=atr1h, mfe=c, tp=tp,
                                                       entry_time=utc_iso(ts), entry_ts=ts, had_alert=had_alert))
                else:
                    log_line(f"Signal detected ({'BUY' if side==1 else 'SELL'} @ {c:.2f}) but 1h ATR "
                             f"unavailable - skipping entry this bar")

        closes_before.append(c)
        state["closes_before"] = list(closes_before)

    # --- price + equity history, one row per processed bar (for the dashboard chart) ---
    base_bal0 = BALANCES[0]
    base_tier = state["tiers"][str(int(base_bal0))]
    pct_return = (base_tier["balance"] / base_bal0 - 1) * 100
    append_csv(PRICE_LOG, PRICE_HEADER,
               [utc_iso(bar["time"]), c, round(pct_return, 4)]
               + [round(state["tiers"][str(int(b))]["balance"], 2) for b in BALANCES]
               + [round(state["tiers"][str(int(b))]["gross_balance"], 2) for b in BALANCES])


def write_summary(state):
    lines = []
    now_utc = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    lines.append("# PAXGUSD ORB - live forward-test summary\n")
    lines.append(f"Last updated: {now_utc}\n")
    lines.append("**Walk-forward validated on PAXGUSD's own 7+ month history (70/30 train/test "
                  "split): trend-following breakout entry with a chandelier trailing stop, up to "
                  f"{MAX_CONCURRENT_TRADES} trades open at once. A holding-time-tiered trading "
                  f"cost is deducted from every trade at close (not a toggle) - {CUT_PCT_UNDER_2H:.0f}% "
                  f"under 2h, {CUT_PCT_2_TO_8H:.0f}% 2-8h, {CUT_PCT_OVER_8H:.0f}% 8h+, of the dollar "
                  "amount risked - train +24.6% CAGR, test +39.7% CAGR, both net of that cost (max "
                  "drawdown -17% to -27%, higher than a single-trade design since concurrent trades "
                  "on the same instrument are correlated, not diversified). Paper trading only, no "
                  "real money involved. Checked on a schedule (see workflow) - notification lag applies.**\n")
    lines.append("\n**Tax note:** figures below are PRE-TAX. PAXGUSD here is a futures contract, "
                  "not a spot crypto buy/sell, so per Delta Exchange's own guidance the flat 30% "
                  "VDA tax + 1% TDS does NOT apply - F&O profit is taxed as regular income at your "
                  "own income-tax slab rate instead, paid separately (via ITR), not deducted per "
                  "trade. Not tax advice - consult a CA for your actual liability.\n")
    lines.append("\n**Margin note:** each trade's position size (notional) averages ~89% of "
                  "balance - that's the leveraged position's full value, not capital locked up. "
                  "At ~10-20x leverage (Delta supports up to 100x on PAXGUSD), actual MARGIN "
                  "locked per trade is only ~4-9% of balance, even with 3 trades open at once "
                  "(~13-27% worst case) - set your leverage in that range on Delta when copying "
                  "these paper trades to keep margin usage sane.\n")

    at, af, anf = state["alerts_total"], state["alerts_followed"], state["alerts_not_followed"]
    resolved = af + anf
    lines.append("## Early-warning indicator accuracy\n")
    lines.append(f"- Alerts fired: {at}\n")
    lines.append(f"- Resolved so far: {resolved} (followed by real breakout: {af}, not followed: {anf})\n")
    if resolved:
        lines.append(f"- Follow-through rate: {af/resolved*100:.1f}%\n")

    tt, tw, tl, sr = state["trades_total"], state["trades_wins"], state["trades_losses"], state["sum_R"]
    lines.append("\n## Trades (paper)\n")
    lines.append(f"- Total: {tt}  |  Wins: {tw}  |  Losses: {tl}\n")
    if tt:
        lines.append(f"- Win rate: {tw/tt*100:.1f}%\n")
        lines.append(f"- Total R: {sr:.2f}  |  Avg R/trade: {sr/tt:.3f}\n")
    twa, wwa, twoa, wwoa = (state["trades_with_alert"], state["wins_with_alert"],
                            state["trades_without_alert"], state["wins_without_alert"])
    if twa:
        lines.append(f"- Trades WITH a prior alert: {twa}, win rate {wwa/twa*100:.1f}%\n")
    if twoa:
        lines.append(f"- Trades WITHOUT a prior alert: {twoa}, win rate {wwoa/twoa*100:.1f}%\n")

    lines.append("\n## Simulated account balances (1% risk per trade, compounding)\n")
    lines.append("Gross = same trades, as if no cut were ever charged. Net = what actually happened, "
                  "cut deducted every close - the real number.\n\n")
    lines.append("| Starting balance | Gross balance | Gross return | Net balance | Net return | "
                  "Max drawdown | Trades | Win rate |\n")
    lines.append("|---|---|---|---|---|---|---|---|\n")
    for b in BALANCES:
        tier = state["tiers"][str(int(b))]
        ret_pct = (tier["balance"] / b - 1) * 100
        gross_ret_pct = (tier["gross_balance"] / b - 1) * 100
        wr = (tier["wins"] / tier["trades"] * 100) if tier["trades"] else 0.0
        lines.append(f"| ${b:,.0f} | ${tier['gross_balance']:,.2f} | {gross_ret_pct:+.2f}% | "
                      f"${tier['balance']:,.2f} | {ret_pct:+.2f}% | "
                      f"{tier['max_dd_pct']:.2f}% | {tier['trades']} | {wr:.1f}% |\n")

    with open(SUMMARY_FILE, "w", encoding="utf-8") as f:
        f.writelines(lines)


SESSION = requests.Session()


def main():
    state = load_state()
    now = int(time.time())

    # Continuous, all-day fetching: pick up right where the last run left off,
    # regardless of what time of day (or NY session) it currently is. The
    # workflow's cron now fires every 5 minutes around the clock, so this
    # normally only needs to cover the last few minutes; the 6-hour fallback
    # only matters on a genuinely first-ever run with no state.json yet
    # (normally state.json is seeded with real history before this ever runs -
    # see the chat/README for the one-time backfill step).
    last_bar_time = state.get("last_bar_time")
    fetch_from = (last_bar_time + 60) if last_bar_time else (now - 6 * 3600)

    rows = fetch_candles(SESSION, "1m", fetch_from, now)
    new_rows = [r for r in rows
                if (last_bar_time is None or r["time"] > last_bar_time) and r["time"] + 60 <= now]

    if not new_rows:
        log_line("No new closed 1-min bars since last run - nothing to do.")
    else:
        for bar in new_rows:
            process_bar(state, bar)
            state["last_bar_time"] = bar["time"]

    write_summary(state)
    save_state(state)


if __name__ == "__main__":
    main()
