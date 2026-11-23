# PAXGUSD ORB - live forward-test summary
Last updated: 2026-11-23 03:08:12 UTC
**Walk-forward validated on PAXGUSD's own 7+ month history (70/30 train/test split): trend-following breakout entry with a chandelier trailing stop, up to 3 trades open at once. A holding-time-tiered trading cost is deducted from every trade at close (not a toggle) - 9% under 2h, 15% 2-8h, 25% 8h+, of the dollar amount risked - train +24.6% CAGR, test +39.7% CAGR, both net of that cost (max drawdown -17% to -27%, higher than a single-trade design since concurrent trades on the same instrument are correlated, not diversified). Paper trading only, no real money involved. Checked on a schedule (see workflow) - notification lag applies.**

**Tax note:** figures below are PRE-TAX. PAXGUSD here is a futures contract, not a spot crypto buy/sell, so per Delta Exchange's own guidance the flat 30% VDA tax + 1% TDS does NOT apply - F&O profit is taxed as regular income at your own income-tax slab rate instead, paid separately (via ITR), not deducted per trade. Not tax advice - consult a CA for your actual liability.

**Margin note:** each trade's position size (notional) averages ~89% of balance - that's the leveraged position's full value, not capital locked up. At ~10-20x leverage (Delta supports up to 100x on PAXGUSD), actual MARGIN locked per trade is only ~4-9% of balance, even with 3 trades open at once (~13-27% worst case) - set your leverage in that range on Delta when copying these paper trades to keep margin usage sane.
## Early-warning indicator accuracy
- Alerts fired: 10
- Resolved so far: 9 (followed by real breakout: 6, not followed: 3)
- Follow-through rate: 66.7%

## Trades (paper)
- Total: 51  |  Wins: 22  |  Losses: 29
- Win rate: 43.1%
- Total R: 36.12  |  Avg R/trade: 0.708
- Trades WITH a prior alert: 6, win rate 0.0%
- Trades WITHOUT a prior alert: 45, win rate 48.9%

## Simulated account balances (1% risk per trade, compounding)
Gross = same trades, as if no cut were ever charged. Net = what actually happened, cut deducted every close - the real number.

| Starting balance | Gross balance | Gross return | Net balance | Net return | Max drawdown | Trades | Win rate |
|---|---|---|---|---|---|---|---|
| $100 | $141.92 | +41.92% | $127.19 | +27.19% | -11.37% | 51 | 43.1% |
| $1,000 | $1,419.22 | +41.92% | $1,271.92 | +27.19% | -11.37% | 51 | 43.1% |
| $10,000 | $14,192.18 | +41.92% | $12,719.15 | +27.19% | -11.37% | 51 | 43.1% |
