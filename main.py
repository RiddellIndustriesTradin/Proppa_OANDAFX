"""
Proppa EUR/USD ORB Paper Trading Bot
Phase 4 Paper Trading via OANDA Practice v20 API
Strategy: EUR/USD 15M ORB with EMA + Volume filters
"""

import os
import json
import logging
from datetime import datetime, timedelta, timezone
import time
from typing import Optional, Dict, List, Tuple

import requests
from requests.auth import HTTPBasicAuth
import telegram

# ===== CONFIGURATION =====
OANDA_BASE_URL = os.getenv("OANDA_BASE_URL", "https://api-fxpractice.oanda.com").strip()
OANDA_ACCOUNT_ID = os.getenv("OANDA_ACCOUNT_ID", "101-011-39004310-001").strip()
OANDA_TOKEN = os.getenv("OANDA_TOKEN", "").strip()
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "8075862544"))

# Strategy Parameters (Phase 2 v2.1 LOCKED)
ORB_START_HOUR, ORB_START_MIN = 7, 0
ORB_END_HOUR, ORB_END_MIN = 7, 30
ENTRY_START_HOUR, ENTRY_START_MIN = 7, 30
ENTRY_END_HOUR, ENTRY_END_MIN = 9, 0
EOD_CLOSE_HOUR, EOD_CLOSE_MIN = 21, 0
TIMEZONE = "GMT"

ORB_MIN, ORB_MAX = 5, 30  # pips
SL_MIN, SL_MAX = 10, 40   # pips
ATR_MULTIPLIER = 1.5
ATR_PERIOD = 14
EMA_PERIOD = 50
VOLUME_SMA_PERIOD = 20
VOLUME_FILTER_MULTIPLIER = 1.5
PAIR = "EUR_USD"
INSTRUMENT = "EUR_USD"
RISK_PERCENT = 0.01  # 1% risk per trade
LOT_SIZE = 0.01  # minimum micro lot
COMMISSION_PIPS = 1.5

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

# ===== TELEGRAM =====
def send_telegram_alert(message: str):
    """Send alert to Telegram (non-blocking)"""
    try:
        if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
            logger.warning("Telegram not configured, skipping alert")
            return
        
        import asyncio
        async def send_async():
            bot = telegram.Bot(token=TELEGRAM_BOT_TOKEN)
            await bot.send_message(
                chat_id=TELEGRAM_CHAT_ID,
                text=message,
                parse_mode=telegram.constants.ParseMode.MARKDOWN
            )
        
        # Run async function without event loop warnings
        try:
            loop = asyncio.get_event_loop()
            if loop.is_closed():
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
        except RuntimeError:
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
        
        loop.run_until_complete(send_async())
        logger.info(f"Telegram alert sent: {message[:50]}...")
    except Exception as e:
        logger.error(f"Telegram error: {e}")

# ===== OANDA API HELPERS =====
def oanda_request(method: str, endpoint: str, data: dict = None) -> dict:
    """Make authenticated request to OANDA v20 API"""
    headers = {
        "Authorization": f"Bearer {OANDA_TOKEN}",
        "Content-Type": "application/json"
    }
    url = f"{OANDA_BASE_URL}{endpoint}"
    
    try:
        if method == "GET":
            response = requests.get(url, headers=headers, timeout=10)
        elif method == "POST":
            response = requests.post(url, headers=headers, json=data, timeout=10)
        else:
            raise ValueError(f"Unsupported method: {method}")
        
        if response.status_code >= 400:
            logger.error(f"OANDA error {response.status_code}: {response.text}")
            return {"error": response.text}
        
        return response.json()
    except Exception as e:
        logger.error(f"Request error: {e}")
        return {"error": str(e)}

def get_candles(count: int = 500, granularity: str = "M15") -> List[dict]:
    """Fetch historical candles from OANDA"""
    endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}/instruments/{INSTRUMENT}/candles"
    params = {
        "count": min(count, 5000),
        "granularity": granularity,
        "price": "MBA"  # mid, bid, ask
    }
    
    headers = {
        "Authorization": f"Bearer {OANDA_TOKEN}",
        "Content-Type": "application/json"
    }
    url = f"{OANDA_BASE_URL}{endpoint}"
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            return data.get("candles", [])
        else:
            logger.error(f"Candle fetch error: {response.status_code} - {response.text}")
            return []
    except Exception as e:
        logger.error(f"Candle fetch exception: {e}")
        return []

def get_account_info() -> dict:
    """Get account details"""
    endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}"
    return oanda_request("GET", endpoint)

def place_order(side: str, units: int, sl_pips: float, tp_pips: float) -> dict:
    """Place market order with stop loss and take profit"""
    endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}/orders"
    
    # Get current price for TP/SL calculation
    ticker = get_ticker()
    if not ticker:
        logger.error("Cannot get current price for order")
        return {"error": "No price data"}
    
    bid = float(ticker.get("bids", [{}])[0].get("price", 0))
    ask = float(ticker.get("asks", [{}])[0].get("price", 0))
    current_price = (bid + ask) / 2
    
    # Calculate TP and SL levels
    if side == "BUY":
        sl_level = current_price - (sl_pips / 10000)
        tp_level = current_price + (tp_pips / 10000)
    else:  # SELL
        sl_level = current_price + (sl_pips / 10000)
        tp_level = current_price - (tp_pips / 10000)
    
    order_data = {
        "order": {
            "type": "MARKET",
            "instrument": INSTRUMENT,
            "units": units if side == "BUY" else -units,
            "takeProfitOnFill": {
                "price": f"{tp_level:.5f}"
            },
            "stopLossOnFill": {
                "price": f"{sl_level:.5f}"
            },
            "timeInForce": "FOK"
        }
    }
    
    response = oanda_request("POST", endpoint, order_data)
    
    if "orderFillTransaction" in response:
        logger.info(f"Order placed: {response['orderFillTransaction']}")
        send_telegram_alert(f"🔥 **ENTRY SIGNAL**\n{side} EUR/USD\nPrice: {current_price:.5f}\nUnits: {units}\nSL: {sl_pips}p | TP: {tp_pips}p")
        return response
    else:
        logger.error(f"Order failed: {response}")
        send_telegram_alert(f"❌ **ORDER FAILED**\n{response.get('error', 'Unknown error')}")
        return response

def get_ticker() -> Optional[dict]:
    """Get current bid/ask prices"""
    endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}/instruments/{INSTRUMENT}/candles"
    params = {"count": 1, "granularity": "M1"}
    
    headers = {
        "Authorization": f"Bearer {OANDA_TOKEN}",
        "Content-Type": "application/json"
    }
    url = f"{OANDA_BASE_URL}{endpoint}"
    
    try:
        response = requests.get(url, headers=headers, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            if data.get("candles"):
                candle = data["candles"][-1]
                return {
                    "bids": [{"price": candle["bid"]["c"]}],
                    "asks": [{"price": candle["ask"]["c"]}]
                }
    except Exception as e:
        logger.error(f"Ticker fetch error: {e}")
    
    return None

def close_all_positions():
    """Close all open positions at EOD"""
    endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}/openPositions"
    positions = oanda_request("GET", endpoint).get("positions", [])
    
    for position in positions:
        if position["instrument"] == INSTRUMENT and position["long"]["units"] != "0":
            # Close long
            close_endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}/positions/{INSTRUMENT}/close"
            close_data = {
                "longUnits": "ALL"
            }
            oanda_request("PUT", close_endpoint, close_data)
            send_telegram_alert("📊 **EOD CLOSE** - Long position closed")
        elif position["instrument"] == INSTRUMENT and position["short"]["units"] != "0":
            # Close short
            close_endpoint = f"/v3/accounts/{OANDA_ACCOUNT_ID}/positions/{INSTRUMENT}/close"
            close_data = {
                "shortUnits": "ALL"
            }
            oanda_request("PUT", close_endpoint, close_data)
            send_telegram_alert("📊 **EOD CLOSE** - Short position closed")

# ===== STRATEGY LOGIC =====
def calculate_atr(candles: List[dict], period: int = 14) -> float:
    """Calculate ATR (Average True Range)"""
    if len(candles) < period:
        return 0
    
    tr_values = []
    for i in range(1, len(candles)):
        h = float(candles[i]["mid"]["h"])
        l = float(candles[i]["mid"]["l"])
        c_prev = float(candles[i-1]["mid"]["c"])
        
        tr = max(h - l, abs(h - c_prev), abs(l - c_prev))
        tr_values.append(tr)
    
    atr = sum(tr_values[-period:]) / period
    return atr

def calculate_ema(candles: List[dict], period: int = 50) -> float:
    """Calculate EMA (Exponential Moving Average)"""
    if len(candles) < period:
        return 0
    
    closes = [float(c["mid"]["c"]) for c in candles]
    
    # Simple SMA for first value
    sma = sum(closes[-period:]) / period
    ema = sma
    
    # EMA calculation
    multiplier = 2 / (period + 1)
    for price in closes[-period:]:
        ema = price * multiplier + ema * (1 - multiplier)
    
    return ema

def calculate_volume_sma(candles: List[dict], period: int = 20) -> float:
    """Calculate Volume SMA"""
    if len(candles) < period:
        return 0
    
    volumes = [int(c.get("volume", 0)) for c in candles[-period:]]
    return sum(volumes) / period if volumes else 0

def check_entry_signal(candles: List[dict]) -> Tuple[bool, str, float, float]:
    """
    Check if entry signal is triggered
    Returns: (signal_triggered, direction, stop_loss_pips, take_profit_pips)
    """
    if len(candles) < 50:
        return False, "", 0, 0
    
    now = datetime.now(timezone.utc)
    
    # Check time window
    hour, minute = now.hour, now.minute
    if not (ENTRY_START_HOUR == hour and ENTRY_START_MIN <= minute < 60) and \
       not (ENTRY_END_HOUR == hour and 0 <= minute < ENTRY_END_MIN):
        return False, "", 0, 0
    
    # Get latest candles
    latest = candles[-1]
    prev = candles[-2]
    
    high_latest = float(latest["mid"]["h"])
    low_latest = float(latest["mid"]["l"])
    close_latest = float(latest["mid"]["c"])
    close_prev = float(prev["mid"]["c"])
    
    # Calculate ORB (opening range break)
    orb_high = max([float(c["mid"]["h"]) for c in candles if is_in_orb_window(c)])
    orb_low = min([float(c["mid"]["l"]) for c in candles if is_in_orb_window(c)])
    orb_range = (orb_high - orb_low) * 10000  # Convert to pips
    
    # Check ORB range validity
    if not (ORB_MIN <= orb_range <= ORB_MAX):
        return False, "", 0, 0
    
    # Calculate indicators
    atr = calculate_atr(candles, ATR_PERIOD)
    atr_pips = atr * 10000
    sl_pips = atr_pips * ATR_MULTIPLIER
    
    # Validate SL bounds
    if not (SL_MIN <= sl_pips <= SL_MAX):
        return False, "", 0, 0
    
    # Volume filter
    vol_sma = calculate_volume_sma(candles, VOLUME_SMA_PERIOD)
    current_volume = candles[-1].get("volume", 0)
    if current_volume < vol_sma * VOLUME_FILTER_MULTIPLIER:
        return False, "", 0, 0
    
    # EMA filter
    ema = calculate_ema(candles, EMA_PERIOD)
    
    # Entry signal: close breaks above ORB high + close > EMA
    if close_latest > (orb_high + 0.0001) and close_latest > ema:
        tp_pips = sl_pips * 1.5  # 1.5:1 risk-reward
        return True, "BUY", sl_pips, tp_pips
    
    # Short signal: close breaks below ORB low + close < EMA
    if close_latest < (orb_low - 0.0001) and close_latest < ema:
        tp_pips = sl_pips * 1.5
        return True, "SELL", sl_pips, tp_pips
    
    return False, "", 0, 0

def is_in_orb_window(candle: dict) -> bool:
    """Check if candle is within ORB window (07:00-07:30 GMT)"""
    try:
        time_str = candle.get("time", "")
        candle_time = datetime.fromisoformat(time_str.replace("Z", "+00:00"))
        
        return (candle_time.hour == ORB_START_HOUR and 
                ORB_START_MIN <= candle_time.minute < ORB_END_HOUR*60 + ORB_END_MIN)
    except:
        return False

# ===== MAIN BOT LOOP =====
def main():
    """Main bot loop"""
    logger.info("🚀 Proppa EUR/USD ORB Bot started!")
    send_telegram_alert("🚀 **BOT STARTED**\nEUR/USD Paper Trading (Phase 4)\nAccount: OANDA Practice")
    
    trade_logged_today = False
    last_check_time = None
    
    while True:
        try:
            now = datetime.now(timezone.utc)
            
            # Log hourly status
            if last_check_time is None or (now - last_check_time).seconds >= 3600:
                account = get_account_info()
                balance = account.get("account", {}).get("balance", "N/A")
                logger.info(f"⏰ Hourly check - Balance: {balance}")
                last_check_time = now
            
            # Fetch latest candles
            candles = get_candles(count=500, granularity="M15")
            if not candles:
                logger.warning("No candles received, retrying...")
                time.sleep(60)
                continue
            
            # Check entry signal
            signal_triggered, direction, sl_pips, tp_pips = check_entry_signal(candles)
            
            if signal_triggered:
                logger.info(f"✅ Entry signal: {direction} | SL: {sl_pips}p | TP: {tp_pips}p")
                
                # Place order
                units = int(LOT_SIZE * 100000)
                order_result = place_order(direction, units, sl_pips, tp_pips)
                
                if "orderFillTransaction" in order_result:
                    trade_logged_today = True
                    logger.info("✅ Order filled successfully")
                else:
                    logger.error(f"❌ Order failed: {order_result}")
            
            # EOD close check
            if now.hour == EOD_CLOSE_HOUR and now.minute >= EOD_CLOSE_MIN:
                if trade_logged_today:
                    close_all_positions()
                    trade_logged_today = False
                    send_telegram_alert("📊 **DAILY SUMMARY**\nEnd of day close executed\nCheck trades in OANDA dashboard")
            
            # Reset daily flag at midnight
            if now.hour == 0 and now.minute == 0:
                trade_logged_today = False
            
            # Sleep 60 seconds before next check
            time.sleep(60)
        
        except KeyboardInterrupt:
            logger.info("🛑 Bot stopped by user")
            send_telegram_alert("🛑 **BOT STOPPED**")
            break
        except Exception as e:
            logger.error(f"❌ Bot error: {e}")
            send_telegram_alert(f"⚠️ **BOT ERROR**\n{str(e)}")
            time.sleep(300)  # Wait 5 min before retry

if __name__ == "__main__":
    main()
