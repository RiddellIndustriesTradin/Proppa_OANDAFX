# Proppa EUR/USD ORB Paper Trading Bot

**Phase 4 Paper Trading | OANDA Practice Account | 30-Day Validation Gate**

---

## Overview

This is a fully automated trading bot for EUR/USD 15M opening range breakout (ORB) strategy.

- **Strategy**: Opening Range Breakout with EMA + Volume filters
- **Asset**: EUR/USD
- **Timeframe**: 15 minutes (M15)
- **Account**: OANDA Practice (Paper Trading)
- **Duration**: 30 days minimum (Phase 4 validation gate)
- **Alerts**: Telegram (8 per trade cycle)
- **Infrastructure**: Railway (24/7 uptime)

---

## Strategy Specification (Phase 2 v2.1 — LOCKED)

### Entry Logic
- **ORB Window**: 07:00–07:30 GMT (calculate high/low)
- **Entry Window**: 07:30–09:00 GMT
- **Signal**: Close breaks ORB high (buy) or ORB low (sell)
- **Filters**:
  - EMA(50) 1H confirmation
  - Volume >= 1.5× SMA(20)
  - ATR(14) within 10–40 pips

### Risk Management
- **Position Size**: 0.01 micro lot (10,000 EUR)
- **Stop Loss**: 1.5× ATR (bounds: 10–40 pips)
- **Take Profit**: 1.5× SL (risk-reward 1:1.5)
- **Risk per Trade**: 1%
- **Daily Loss Cap**: 5%
- **Max Drawdown**: 10%

### Exit Rules
- **EOD Close**: 21:00 GMT (mandatory, no overnight holds)
- **Max 1 Trade**: Per calendar day
- **Commission**: 1.5 pips (OANDA standard)

---

## Deployment (Railway)

### 1. Create GitHub Repository

```bash
cd /Users/proppa_tru_mac/Desktop/Projects/01_Proppa_EUR_USD_Bot
git init
git add .
git commit -m "Initial EUR/USD bot commit"
git remote add origin https://github.com/YOUR_USERNAME/proppa-eur-usd-bot.git
git push -u origin main
```

### 2. Connect to Railway

1. Go to **Railway Dashboard** → **LIVE PAPER TRADE BOT** project
2. Click **+ New Service** → **GitHub Repo** → Select your repo
3. Name the service: `proppa-eur-usd-bot`
4. Click **Deploy**

### 3. Set Environment Variables

In Railway dashboard, go to the service variables:

```
OANDA_BASE_URL = https://api-fxpractice.oanda.com
OANDA_ACCOUNT_ID = 101-011-39004310-001
OANDA_TOKEN = [your-practice-api-token]
TELEGRAM_BOT_TOKEN = 8680466883:AAEGonHeFcD-yaw0QMjh4XalTXXa4ULqdAk
TELEGRAM_CHAT_ID = 8075862544
```

**NEVER hardcode tokens in code — always use environment variables!**

### 4. Deploy

Click **Deploy** on the service. Railway will:
- Install dependencies (`requirements.txt`)
- Run `python main.py` (from `Procfile`)
- Keep the bot running 24/7

---

## Local Testing (Before Deployment)

### Setup

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# Install dependencies
pip install -r requirements.txt

# Create .env file
cp .env.example .env
# Edit .env and add your credentials
```

### Run Locally

```bash
python main.py
```

You should see:
```
2026-04-12 10:15:30 - INFO - 🚀 Proppa EUR/USD ORB Bot started!
2026-04-12 10:15:31 - INFO - Telegram alert sent: 🚀 **BOT STARTED**...
```

### Check Logs

```bash
tail -f /tmp/eur_usd_bot.log
```

---

## Monitoring (Phase 4)

### Telegram Alerts

The bot sends 8 alerts per trade cycle:

1. **Bot Start** — System online
2. **Entry Signal** — Order placed (buy/sell + price + SL/TP)
3. **Order Confirmation** — Fill confirmed
4. **EOD Warning** — 30 min before close (if position open)
5. **EOD Close** — Position closed at 21:00 GMT
6. **Daily Summary** — Trade stats (PnL, win rate, DD)
7. **System Error** — Any failures (API, connectivity)
8. **Bot Stop** — Graceful shutdown

### Trade Log

Check `/tmp/eur_usd_bot.log` for:
- Entry/exit prices
- P&L per trade
- System health checks
- Indicator values (ATR, EMA, volume)

### OANDA Dashboard

Monitor real-time:
- Open positions
- Trade history
- Account balance
- Equity curve

---

## Phase 4 Validation Gates (30 Days)

The bot will be evaluated against:

| Gate | Target | Minimum |
|------|--------|---------|
| **Duration** | 30 days | Hard requirement |
| **Trades** | 20+ | Achievable (2/day avg) |
| **Win Rate** | ≥55% | 1.2–1.5 PF acceptable |
| **Profit Factor** | 1.5 | 1.2+ acceptable if uptrend |
| **Max Drawdown** | ≤10% | Hard stop (zero exceptions) |
| **System Uptime** | 99.9% | Zero missed alerts |

**If all gates pass** → Transition to LIVE account (`001-011-21182173-001`)

---

## Troubleshooting

### Bot Not Sending Alerts

- Check `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in Railway variables
- Verify bot exists in Telegram (@Proppa_OANDAFX)
- Check `/tmp/eur_usd_bot.log` for connection errors

### No Trades Triggering

- Check time window (entry must be 07:30–09:00 GMT)
- Verify ORB range (must be 5–30 pips)
- Check volume filter (current vol >= 1.5× SMA20)
- Check EMA(50) confirmation
- Review logs for indicator values

### OANDA API Errors

- Verify `OANDA_TOKEN` is valid (not expired)
- Check `OANDA_ACCOUNT_ID` matches practice account
- Ensure account has $1,000+ balance
- Check API rate limits (max 240 requests/min)

---

## Files

- **main.py** — Core trading logic
- **requirements.txt** — Python dependencies
- **Procfile** — Railway configuration
- **README.md** — This file

---

## Support

Issues? Check:
1. Railway logs (dashboard → service → logs)
2. Bot logs (`/tmp/eur_usd_bot.log`)
3. Telegram chat (bot should confirm startup)
4. OANDA account (manual trade verification)

---

**Bot Status**: 🟢 Ready for Phase 4 Paper Trading  
**Last Updated**: Sunday, April 12, 2026  
**Strategy Version**: Phase 2 v2.1 (Locked)
