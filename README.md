# Coinbase 10-Minute Trading Bot

Looks for buys every 10 minutes on BTC-USD and ETH-USD and checks exits every minute.
Each trade is $25. Daily loss limit is $5. It starts in **paper mode (fake money)**.

## How it trades
- **Buy:** the 9 EMA crosses above the 21 EMA, price is above the 100 EMA (uptrend), and RSI is under 70
- **Sell:** at -2% (stop-loss), +3.5% (take-profit), a 1% trailing stop once up 2%, or when the trend flips down
- **Protection:** stops new buys for the day after a $5 net loss, holds at most 2 positions, waits 30 minutes after a stop-loss, and won't start if your take-profit can't beat fees

## Deploy on Railway
1. In Railway, go to New Project → Deploy from GitHub repo → pick this repo.
2. Add a **Volume** mounted at `/data`. This keeps positions and trade history across restarts.
3. Under **Variables**, paste what you need from `.env.example`. Paper mode needs no keys.
4. Deploy. The logs show every check, buy and sell.

## Going live (real money)
Run paper mode for 1–2 weeks first. Download `/data/trades.csv` and check the totals.
1. Create a Coinbase **Advanced Trade** API key with **View + Trade** only. Never give it **Transfer**.
2. Add `COINBASE_API_KEY` and `COINBASE_API_SECRET` in Railway.
3. Set `PAPER_TRADING=false` and `LIVE_TRADING_CONFIRM=yes`, then redeploy.
4. Keep only the amount you're willing to trade in that Coinbase account.

## Stop it
In Railway, set `PAPER_TRADING=true` or remove the deployment. Any open position stays in your
Coinbase account, where you can sell it manually.

## Files
- `/data/trades.csv`: every closed trade with its P&L
- `/data/state.json`: open positions, today's P&L, and win/loss count
