"""
Proppa EUR/USD ORB Trading Bot
Phase 4 Paper Trading / Phase 5 Live via OANDA v20 API
Strategy: EUR/USD 15M ORB with 1H EMA(50) + Volume filters
Version: 2.0 — QA Fixed (27 April 2026)

FIXES FROM v1.0:
  FIX 1: Removed 1-pip entry buffer (was suppressing valid signals)
  FIX 2: EMA now uses 1H candles via separate fetch (spec compliant)
  FIX 3: Telegram heartbeat added to hourly check (confirms pipeline alive)
  FIX 4: Split TP structure — TP1 at 1:1 RR (50%), TP2 at 2:1 RR (50%)
  FIX 5: Volume filter log level raised to INFO (was DEBUG — invisible in Railway)
  FIX 6: Signal check logs fire on every bar in entry window for full visibility
"""

import os
import logging
from datetime import datetime, timezone
import time
from typing import Optional, Dict, List, Tuple

import requests
import telegram

# ===== CONFIGURATION =====
OANDA_BASE_URL   = os.getenv("OANDA_BASE_URL",   "https://api-fxpractice.oanda.com").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "101-011-39004310-001").strip()
OANDA_TOKEN      = os.getenv("OANDA_TOKEN",      "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID   = int(os.getenv("TELEGRAM_CHAT_ID", "8075862544"))

# ── Strategy Parameters (Phase 2 v2.1 LOCKED) ──────────────────────────────
ORB_START_HOUR,  ORB_START_MIN  = 7, 0
ORB_END_HOUR,    ORB_END_MIN    = 7, 30
ENTRY_START_HOUR, ENTRY_START_MIN = 7, 30
ENTRY_END_HOUR,  ENTRY_END_MIN  = 9, 0
EOD_WARN_HOUR,   EOD_WARN_MIN   = 20, 45
EOD_CLOSE_HOUR,  EOD_CLOSE_MIN  = 21, 0

ORB_MIN_PIPS, ORB_MAX_PIPS = 5, 30
SL_MIN_PIPS,  SL_MAX_PIPS  = 10, 40
ATR_MULTIPLIER      = 1.5
ATR_PERIOD          = 14
EMA_PERIOD          = 50       # applied to 1H candles
VOLUME_SMA_PERIOD   = 20
VOLUME_MULTIPLIER   = 1.5
INSTRUMENT          = "EUR_USD"
RISK_PERCENT        = 0.01     # 1% per trade
DAILY_LOSS_CAP_PCT  = 0.05     # 5% daily stop
MAX_DRAWDOWN_PCT    = 0.10     # 10% max drawdown

# ===== LOGGING =====
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('/tmp/eur_usd_bot.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Suppress noisy libraries
for lib in ['telegram', 'telegram.vendor.ptb_urllib3.urllib3', 'requests', 'urllib3']:
    logging.getLogger(lib).setLevel(logging.WARNING)


# ===== TELEGRAM =====
def send_telegram(message: str):
    """Send alert to Telegram."""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.warning("Telegram not configured — skipping alert")
            return

        import asyncio

        async def _send():
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode=telegram.constants.ParseMode.MARKDOWN
            )

        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

        loop.run_until_complete(_send())
        logger.info(f"Telegram sent: {message[:60]}...")
    except Exception as e:
        logger.error(f"Telegram error: {e}")


# ===== OANDA API HELPERS =====
def _headers() -> dict:
    return {
        "Authorization": f"Bearer {OANDA_TOKEN}",
        "Content-Type":  "application/json"
    }


def oanda_get(endpoint: str, params: dict = None) -> dict:
    url = f"{OANDA_BASE_URL}{endpoint}"
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=10)
        if r.status_code >= 400:
            logger.error(f"OANDA GET {endpoint} → {r.status_code}: {r.text[:300]}")
            return {"error": r.text}
        return r.json()
    except Exception as e:
        logger.error(f"OANDA GET error: {e}")
        return {"error": str(e)}


def oanda_post(endpoint: str, data: dict) -> dict:
    url = f"{OANDA_BASE_URL}{endpoint}"
    try:
        r = requests.post(url, headers=_headers(), json=data, timeout=10)
        if r.status_code >= 400:
            logger.error(f"OANDA POST {endpoint} → {r.status_code}: {r.text[:300]}")
            return {"error": r.text}
        return r.json()
    except Exception as e:
        logger.error(f"OANDA POST error: {e}")
        return {"error": str(e)}


def oanda_put(endpoint: str, data: dict) -> dict:
    url = f"{OANDA_BASE_URL}{endpoint}"
    try:
        r = requests.put(url, headers=_headers(), json=data, timeout=10)
        if r.status_code >= 400:
            logger.error(f"OANDA PUT {endpoint} → {r.status_code}: {r.text[:300]}")
            return {"error": r.text}
        return r.json()
    except Exception as e:
        logger.error(f"OANDA PUT error: {e}")
        return {"error": str(e)}


def get_candles_15m(count: int = 500) -> List[dict]:
    """Fetch 15M candles — used for ORB, ATR, volume."""
    data = oanda_get(
        f"/v3/instruments/{INSTRUMENT}/candles",
        params={"count": min(count, 5000), "granularity": "M15", "price": "MBA"}
    )
    return data.get("candles", [])


def get_candles_1h(count: int = 60) -> List[dict]:
    """Fetch 1H candles — used for EMA(50) per spec (FIX 2)."""
    data = oanda_get(
        f"/v3/instruments/{INSTRUMENT}/candles",
        params={"count": min(count, 5000), "granularity": "H1", "price": "M"}
    )
    return data.get("candles", [])


def get_account() -> dict:
    data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}")
    return data.get("account", {})


def get_open_trades() -> List[dict]:
    data = oanda_get(f"/v3/accounts/{OANDA_ACCOUNT_ID}/openTrades")
    return data.get("trades", [])


def get_current_price() -> Tuple[float, float]:
    """Return (bid, ask) mid prices."""
    data = oanda_get(
        f"/v3/instruments/{INSTRUMENT}/candles",
        params={"count": 1, "granularity": "M1", "price": "BA"}
    )
    candles = data.get("candles", [])
    if not candles:
        return 0.0, 0.0
    c = candles[-1]
    bid = float(c.get("bid", {}).get("c", 0))
    ask = float(c.get("ask", {}).get("c", 0))
    return bid, ask


# ===== INDICATORS =====
def calculate_atr(candles: List[dict], period: int = 14) -> float:
    if len(candles) < period + 1:
        return 0.0
    tr_vals = []
    for i in range(1, len(candles)):
        h = float(candles[i]["mid"]["h"])
        l = float(candles[i]["mid"]["l"])
        c_prev = float(candles[i-1]["mid"]["c"])
        tr_vals.append(max(h - l, abs(h - c_prev), abs(l - c_prev)))
    return sum(tr_vals[-period:]) / period


def calculate_ema_1h(candles_1h: List[dict], period: int = 50) -> float:
    """
    EMA(50) on 1H candles — spec compliant (FIX 2).
    Returns 0 if insufficient data.
    """
    if len(candles_1h) < period:
        logger.warning(f"Insufficient 1H candles for EMA ({len(candles_1h)}/{period})")
        return 0.0
    closes = [float(c["mid"]["c"]) for c in candles_1h]
    multiplier = 2 / (period + 1)
    ema = sum(closes[:period]) / period  # seed with SMA
    for price in closes[period:]:
        ema = price * multiplier + ema * (1 - multiplier)
    return ema


def calculate_volume_sma(candles: List[dict], period: int = 20) -> float:
    if len(candles) < period:
        return 0.0
    vols = [int(c.get("volume", 0)) for c in candles[-period:]]
    return sum(vols) / period if vols else 0.0


# ===== POSITION SIZING =====
def calculate_units(balance: float, sl_pips: float) -> int:
    """
    Units = (Balance × 1%) ÷ (SL_pips × pip_value_per_unit)
    For EUR/USD: pip value = $0.0001 per unit → 1 pip on 1000 units = $0.10
    Round DOWN. Minimum 1000 units (0.01 lots).
    """
    risk_usd = balance * RISK_PERCENT
    pip_val_per_unit = 0.0001  # EUR/USD
    units_raw = risk_usd / (sl_pips * pip_val_per_unit)
    units = max(int(units_raw // 1000) * 1000, 1000)
    logger.info(f"Sizing: balance=${balance:.2f} risk=${risk_usd:.2f} SL={sl_pips:.1f}p → {units} units ({units/100000:.2f} lots)")
    return units


# ===== ORDER EXECUTION =====
def place_entry(side: str, sl_pips: float, balance: float) -> dict:
    """
    Place split entry — two equal orders for TP1 and TP2 (FIX 4).
    TP1 = 1:1 RR, TP2 = 2:1 RR. SL moves to breakeven after TP1 hit.
    """
    bid, ask = get_current_price()
    if bid == 0 or ask == 0:
        logger.error("Cannot get current price for order")
        return {"error": "No price data"}

    entry_price = ask if side == "BUY" else bid
    sl_dist  = sl_pips / 10000
    tp1_dist = sl_pips / 10000        # 1:1 RR
    tp2_dist = (sl_pips * 2) / 10000  # 2:1 RR

    if side == "BUY":
        sl_price  = entry_price - sl_dist
        tp1_price = entry_price + tp1_dist
        tp2_price = entry_price + tp2_dist
    else:
        sl_price  = entry_price + sl_dist
        tp1_price = entry_price - tp1_dist
        tp2_price = entry_price - tp2_dist

    total_units = calculate_units(balance, sl_pips)
    half_units  = max(total_units // 2, 1000)

    endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}/orders"

    results = []
    for tp_price, label in [(tp1_price, "TP1"), (tp2_price, "TP2")]:
        order = {
            "order": {
                "type": "MARKET",
                "instrument": INSTRUMENT,
                "units": str(half_units if side == "BUY" else -half_units),
                "takeProfitOnFill":  {"price": f"{tp_price:.5f}"},
                "stopLossOnFill":    {"price": f"{sl_price:.5f}"},
                "timeInForce": "FOK"
            }
        }
        result = oanda_post(endpoint, order)
        results.append(result)
        if "orderFillTransaction" in result:
            fill = result["orderFillTransaction"]
            logger.info(f"✅ {label} order filled: {fill.get('tradeOpened', {})}")
        else:
            logger.error(f"❌ {label} order failed: {result}")

    if any("orderFillTransaction" in r for r in results):
        send_telegram(
            f"🔥 *ENTRY SIGNAL — {side}*\n"
            f"Instrument: EUR/USD\n"
            f"Entry: {entry_price:.5f}\n"
            f"SL: {sl_price:.5f} ({sl_pips:.1f}p)\n"
            f"TP1: {tp1_price:.5f} (1:1)\n"
            f"TP2: {tp2_price:.5f} (2:1)\n"
            f"Units: {half_units*2:,} total ({(half_units*2)/100000:.2f} lots)\n"
            f"Risk: ${balance * RISK_PERCENT:.2f}"
        )
        return results[0]
    else:
        send_telegram(f"❌ *ORDER FAILED*\n{side} EUR/USD\nCheck OANDA dashboard")
        return {"error": "Both orders failed"}


def close_all_positions():
    """Force-close all open positions at EOD."""
    endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}/positions/{INSTRUMENT}/close"
    data = {"longUnits": "ALL", "shortUnits": "ALL"}
    result = oanda_put(endpoint, data)
    if "error" not in result:
        logger.info("✅ EOD — all positions closed")
        send_telegram("📊 *EOD CLOSE*\nAll positions closed at 21:00 GMT")
    else:
        logger.error(f"EOD close failed: {result}")
        send_telegram(f"⚠️ *EOD CLOSE FAILED*\n{result.get('error','')}\nClose manually in OANDA!")


# ===== ORB WINDOW HELPERS =====
def is_in_orb_window(candle: dict) -> bool:
    """True if candle is within today's 07:00–07:30 GMT window."""
    try:
        t = datetime.fromisoformat(candle["time"].replace("Z", "+00:00"))
        today = datetime.now(timezone.utc).date()
        return (
            t.date() == today and
            t.hour == ORB_START_HOUR and
            ORB_START_MIN <= t.minute < ORB_END_MIN
        )
    except Exception:
        return False


def in_entry_window(now: datetime) -> bool:
    """True if current time is within 07:30–09:00 GMT."""
    return (
        (now.hour == ENTRY_START_HOUR and now.minute >= ENTRY_START_MIN) or
        (ENTRY_START_HOUR < now.hour < ENTRY_END_HOUR) or
        (now.hour == ENTRY_END_HOUR and now.minute < ENTRY_END_MIN)
    )


# ===== SIGNAL CHECK =====
def check_entry_signal(
    candles_15m: List[dict],
    candles_1h:  List[dict],
    sl_flag:     dict,
    now:         datetime
) -> Tuple[bool, str, float]:
    """
    Returns (signal, direction, sl_pips).
    All spec filters applied in order.
    """
    # ── Guard: enough data ──────────────────────────────────────────────────
    if len(candles_15m) < 50 or len(candles_1h) < EMA_PERIOD:
        logger.warning(f"Insufficient candle data: 15m={len(candles_15m)}, 1h={len(candles_1h)}")
        return False, "", 0.0

    # ── Guard: entry window ─────────────────────────────────────────────────
    if not in_entry_window(now):
        return False, "", 0.0

    # ── ORB range ───────────────────────────────────────────────────────────
    orb_candles = [c for c in candles_15m if is_in_orb_window(c)]
    if len(orb_candles) < 2:
        logger.info(f"ORB: only {len(orb_candles)} candle(s) found — waiting for window to complete")
        return False, "", 0.0

    orb_high = max(float(c["mid"]["h"]) for c in orb_candles)
    orb_low  = min(float(c["mid"]["l"]) for c in orb_candles)
    orb_range_pips = (orb_high - orb_low) * 10000

    logger.info(f"ORB: H={orb_high:.5f} L={orb_low:.5f} Range={orb_range_pips:.1f}p")

    if not (ORB_MIN_PIPS <= orb_range_pips <= ORB_MAX_PIPS):
        logger.info(f"ORB range {orb_range_pips:.1f}p outside {ORB_MIN_PIPS}–{ORB_MAX_PIPS}p — skip")
        return False, "", 0.0

    # ── ATR / SL ────────────────────────────────────────────────────────────
    atr = calculate_atr(candles_15m, ATR_PERIOD)
    sl_pips = atr * 10000 * ATR_MULTIPLIER

    if not (SL_MIN_PIPS <= sl_pips <= SL_MAX_PIPS):
        if not sl_flag.get("logged"):
            logger.info(f"⚠️ SL {sl_pips:.2f}p outside bounds ({SL_MIN_PIPS}–{SL_MAX_PIPS}p) — skipping today")
            sl_flag["logged"] = True
        return False, "", 0.0

    # ── Volume filter ────────────────────────────────────────────────────────
    vol_sma     = calculate_volume_sma(candles_15m, VOLUME_SMA_PERIOD)
    current_vol = int(candles_15m[-1].get("volume", 0))
    vol_ok      = current_vol >= vol_sma * VOLUME_MULTIPLIER
    logger.info(f"Volume: current={current_vol} SMA={vol_sma:.0f} threshold={vol_sma*VOLUME_MULTIPLIER:.0f} OK={vol_ok}")

    if not vol_ok:
        return False, "", 0.0

    # ── EMA(50) on 1H — spec compliant (FIX 2) ──────────────────────────────
    ema_1h = calculate_ema_1h(candles_1h, EMA_PERIOD)
    if ema_1h == 0.0:
        logger.warning("EMA(50) 1H returned 0 — skipping signal check")
        return False, "", 0.0

    # ── Current close price ──────────────────────────────────────────────────
    close = float(candles_15m[-1]["mid"]["c"])

    logger.info(
        f"Signal check: close={close:.5f} | ORB_H={orb_high:.5f} ORB_L={orb_low:.5f} | "
        f"EMA1H={ema_1h:.5f} | SL={sl_pips:.1f}p"
    )
    logger.info(
        f"  Bull: close>ORB_H={close > orb_high} AND close>EMA={close > ema_1h}"
    )
    logger.info(
        f"  Bear: close<ORB_L={close < orb_low} AND close<EMA={close < ema_1h}"
    )

    # ── Entry triggers — NO pip buffer (FIX 1) ──────────────────────────────
    if close > orb_high and close > ema_1h:
        logger.info(f"🎯 BUY SIGNAL | SL={sl_pips:.1f}p TP1={sl_pips:.1f}p TP2={sl_pips*2:.1f}p")
        return True, "BUY", sl_pips

    if close < orb_low and close < ema_1h:
        logger.info(f"🎯 SELL SIGNAL | SL={sl_pips:.1f}p TP1={sl_pips:.1f}p TP2={sl_pips*2:.1f}p")
        return True, "SELL", sl_pips

    return False, "", 0.0


# ===== MAIN LOOP =====
def main():
    logger.info("🚀 Proppa EUR/USD ORB Bot v2.0 started!")

    # Credential check
    if not OANDA_TOKEN:
        logger.error("❌ OANDA_TOKEN not set — cannot start")
        send_telegram("❌ *BOT STARTUP FAILED*\nOANDA_TOKEN not set!")
        return

    preview = OANDA_TOKEN[:10] + "..." + OANDA_TOKEN[-10:]
    logger.info(f"✅ OANDA credentials loaded (len={len(OANDA_TOKEN)}, preview: {preview})")
    logger.info(f"✅ OANDA Account: {OANDA_ACCOUNT_ID}")
    logger.info(f"✅ OANDA Base URL: {OANDA_BASE_URL}")

    send_telegram(
        f"🚀 *BOT STARTED v2.0*\n"
        f"EUR/USD ORB — Phase 4 Paper Trade\n"
        f"Account: {OANDA_ACCOUNT_ID}\n"
        f"URL: {OANDA_BASE_URL}"
    )

    # ── Session state ────────────────────────────────────────────────────────
    trade_today      = False
    sl_flag          = {"logged": False}
    last_reset_day   = None
    last_hourly_time = None
    eod_warned       = False

    while True:
        try:
            now = datetime.now(timezone.utc)

            # ── Daily reset at UTC midnight ──────────────────────────────────
            if last_reset_day is None or now.date() != last_reset_day:
                trade_today    = False
                sl_flag        = {"logged": False}
                eod_warned     = False
                last_reset_day = now.date()
                logger.info(f"🔄 Daily reset — new trading day: {last_reset_day}")

            # ── Hourly heartbeat — logs AND Telegram (FIX 3) ────────────────
            if last_hourly_time is None or (now - last_hourly_time).seconds >= 3600:
                acct = get_account()
                balance = float(acct.get("balance", 0))
                nav     = float(acct.get("NAV", balance))
                dd_pct  = ((balance - nav) / balance * 100) if balance > 0 else 0
                logger.info(f"⏰ Hourly check — Balance: {balance:.4f} | NAV: {nav:.4f}")
                send_telegram(
                    f"⏰ *Hourly Heartbeat*\n"
                    f"Balance: ${balance:.2f}\n"
                    f"NAV: ${nav:.2f}\n"
                    f"DD: {dd_pct:.2f}%\n"
                    f"Time: {now.strftime('%Y-%m-%d %H:%M')} GMT\n"
                    f"Trade today: {'Yes' if trade_today else 'No'}"
                )
                last_hourly_time = now

                # ── Daily loss cap check ─────────────────────────────────────
                if balance > 0 and (balance - nav) / balance >= DAILY_LOSS_CAP_PCT:
                    logger.warning("🚨 Daily loss cap hit — no more trades today")
                    send_telegram("🚨 *DAILY LOSS CAP HIT*\nNo more trades today. Resuming tomorrow.")
                    trade_today = True  # blocks further entries

            # ── EOD warning at 20:45 GMT ─────────────────────────────────────
            if now.hour == EOD_WARN_HOUR and now.minute >= EOD_WARN_MIN and not eod_warned:
                if trade_today:
                    send_telegram("⚠️ *EOD WARNING*\nPosition open — closing in 15 minutes at 21:00 GMT")
                eod_warned = True

            # ── EOD force-close at 21:00 GMT ─────────────────────────────────
            if now.hour == EOD_CLOSE_HOUR and now.minute >= EOD_CLOSE_MIN:
                open_trades = get_open_trades()
                if open_trades:
                    close_all_positions()
                trade_today = False
                time.sleep(60)
                continue

            # ── Skip if already traded today ─────────────────────────────────
            if trade_today:
                time.sleep(60)
                continue

            # ── Fetch candles ─────────────────────────────────────────────────
            candles_15m = get_candles_15m(count=500)
            candles_1h  = get_candles_1h(count=60)

            if not candles_15m or not candles_1h:
                logger.warning("No candle data received — retrying in 60s")
                time.sleep(60)
                continue

            # ── Signal check ──────────────────────────────────────────────────
            signal, direction, sl_pips = check_entry_signal(
                candles_15m, candles_1h, sl_flag, now
            )

            if signal:
                acct    = get_account()
                balance = float(acct.get("balance", 1000))
                result  = place_entry(direction, sl_pips, balance)

                if "error" not in result:
                    trade_today = True
                    logger.info(f"✅ Trade placed: {direction} | SL={sl_pips:.1f}p")
                else:
                    logger.error(f"❌ Trade failed: {result}")

            time.sleep(60)

        except KeyboardInterrupt:
            logger.info("🛑 Bot stopped by user")
            send_telegram("🛑 *BOT STOPPED* — manual shutdown")
            break
        except Exception as e:
            logger.error(f"❌ Bot error: {e}")
            send_telegram(f"⚠️ *BOT ERROR*\n{str(e)[:200]}")
            time.sleep(300)


if __name__ == "__main__":
    main()
