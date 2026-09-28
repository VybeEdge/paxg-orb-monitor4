# PAXGUSD ORB - live forward-test summary
Last updated: 2026-09-28 16:35:02 UTC
**Walk-forward validated on PAXGUSD's own 7+ month history (70/30 train/test split): trend-following breakout entry with a chandelier trailing stop, up to 3 trades open at once. A holding-time-tiered trading cost is deducted from every trade at close (not a toggle) - 9% under 2h, 15% 2-8h, 25% 8h+, of the dollar amount risked - train +24.6% CAGR, test +39.7% CAGR, both net of that cost (max drawdown -17% to -27%, higher than a single-trade design since concurrent trades on the same instrument are correlated, not diversified). Paper trading only, no real money involved. Checked on a schedule (see workflow) - notification lag applies.**

**Tax note:** figures below are PRE-TAX. PAXGUSD here is a futures contract, not a spot crypto buy/sell, so per Delta Exchange's own guidance the flat 30% VDA tax + 1% TDS does NOT apply - F&O profit is taxed as regular income at your own income-tax slab rate instead, paid separately (via ITR), not deducted per trade. Not tax advice - consult a CA for your actual liability.

**Margin note:** each trade's position size (notional) averages ~89% of balance - that's the leveraged position's full value, not capital locked up. At ~10-20x leverage (Delta supports up to 100x on PAXGUSD), actual MARGIN locked per trade is only ~4-9% of balance, even with 3 trades open at once (~13-27% worst case) - set your leverage in that range on Delta when copying these paper trades to keep margin usage sane.
## Early-warning indicator accuracy
- Alerts fired: 43
- Resolved so far: 43 (followed by real breakout: 15, not followed: 28)
- Follow-through rate: 34.9%

## Trades (paper)
- Total: 146  |  Wins: 50  |  Losses: 96
- Win rate: 34.2%
- Total R: 50.48  |  Avg R/trade: 0.346
- Trades WITH a prior alert: 13, win rate 30.8%
- Trades WITHOUT a prior alert: 133, win rate 34.6%

## Simulated account balances (1% risk per trade, compounding)
| Starting balance | Current balance | Return | Max drawdown | Trades | Win rate |
|---|---|---|---|---|---|
| $100 | $116.52 | +16.52% | -26.61% | 146 | 34.2% |
| $1,000 | $1,165.23 | +16.52% | -26.61% | 146 | 34.2% |
| $10,000 | $11,652.25 | +16.52% | -26.61% | 146 | 34.2% |
