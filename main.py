"""
Proppa Kraken Crypto Bot
Production-ready Kraken Futures trading bot with TradingView webhook integration.
Supertrend + RSI strategy with full risk management.
"""

import os
import sys
import logging
import json
import signal
from datetime import datetime, timedelta
from typing import Dict, Tuple

from flask import Flask, request, jsonify
import yaml
from dotenv import load_dotenv

# Import bot modules
from signal_parser import SignalParser
from kraken_api import KrakenAPI
from position_sizing import PositionSizer
from risk_manager import RiskManager
from telegram_alerts import TelegramAlerter
from trade_logger import TradeLogger

# Load environment variables
load_dotenv()

# Ensure logs directory exists before logging setup
# Railway filesystem won't have this directory pre-created
os.makedirs('logs', exist_ok=True)

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    handlers=[
        logging.FileHandler('logs/bot.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False


class TradingBot:
    """Main trading bot orchestrator."""
    
    def __init__(self, config_path: str = 'config.yaml'):
        """
        Initialize bot.
        
        Args:
            config_path: Path to configuration YAML
        """
        self.config = self._load_config(config_path)
        
        # Initialize components
        self.kraken = KrakenAPI(
            api_key=self.config['kraken']['api_key'],
            api_secret=self.config['kraken']['api_secret'],
            sandbox=self.config['kraken'].get('sandbox', False),
        )
        
        self.position_sizer = PositionSizer(
            risk_per_trade=self.config['trading']['risk_per_trade']
        )
        
        self.risk_manager = RiskManager(
            max_daily_trades=self.config['trading']['max_daily_trades'],
            max_consecutive_losses=self.config['trading']['max_consecutive_losses'],
            max_daily_loss=self.config['trading']['max_daily_loss'],
            max_drawdown=self.config['trading']['max_drawdown'],
            max_drawdown_hard_stop=self.config['trading']['max_drawdown_hard_stop'],
        )
        
        self.alerter = TelegramAlerter(
            bot_token=self.config['telegram']['bot_token'],
            chat_id=self.config['telegram']['chat_id'],
        )
        
        self.logger = TradeLogger('trades.csv')
        self.signal_parser = SignalParser()
        
        # Open positions tracker
        self.positions_state_file = "positions_state.json"
        self.open_positions = self._load_positions()  # Load on startup
        self.last_updated = None
        
        logger.info("✓ Trading Bot initialized")
    
    def _load_config(self, config_path: str) -> Dict:
        """Load configuration from YAML."""
        try:
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)
            
            # Load API keys from environment
            config['kraken']['api_key'] = os.getenv('KRAKEN_API_KEY', config['kraken'].get('api_key'))
            config['kraken']['api_secret'] = os.getenv('KRAKEN_API_SECRET', config['kraken'].get('api_secret'))
            config['telegram']['bot_token'] = os.getenv('TELEGRAM_BOT_TOKEN', config['telegram'].get('bot_token'))
            config['telegram']['chat_id'] = os.getenv('TELEGRAM_CHAT_ID', config['telegram'].get('chat_id'))
            
            # Validate required fields
            if not config['kraken']['api_key']:
                raise ValueError("KRAKEN_API_KEY not set")
            if not config['kraken']['api_secret']:
                raise ValueError("KRAKEN_API_SECRET not set")
            
            logger.info(f"✓ Config loaded from {config_path}")
            return config
        
        except Exception as e:
            logger.error(f"Failed to load config: {str(e)}")
            raise
    
    def _save_positions(self):
        """Persist open positions to JSON file"""
        try:
            with open(self.positions_state_file, 'w') as f:
                # Convert positions dict to JSON-serializable format
                positions_json = {}
                for symbol, trade in self.open_positions.items():
                    positions_json[symbol] = {
                        'entry_price': float(trade.get('entry_price', 0)),
                        'entry_time': str(trade.get('entry_time', '')),
                        'symbol': trade.get('symbol', ''),
                        'side': trade.get('side', ''),
                        'quantity': float(trade.get('quantity', 0)),
                        'sl': float(trade.get('sl', 0)),
                        'tp': float(trade.get('tp', 0)),
                        'bars_held': int(trade.get('bars_held', 0)),
                        'sl_order_id': trade.get('sl_order_id'),
                    }
                json.dump(positions_json, f, indent=2)
                logger.debug(f"Saved {len(positions_json)} open positions")
        except Exception as e:
            logger.error(f"Failed to save positions state: {e}")
    
    def _load_positions(self):
        """Load open positions from JSON file on startup"""
        if not os.path.exists(self.positions_state_file):
            return {}
        
        try:
            with open(self.positions_state_file, 'r') as f:
                positions_json = json.load(f)
                positions = {}
                for symbol, trade_data in positions_json.items():
                    # Parse entry_time from ISO format string back to datetime
                    entry_time_str = trade_data.get('entry_time')
                    try:
                        entry_time = datetime.fromisoformat(entry_time_str) if entry_time_str else None
                    except:
                        entry_time = None
                    
                    positions[symbol] = {
                        'entry_price': trade_data.get('entry_price'),
                        'entry_time': entry_time,  # Now a datetime object
                        'symbol': trade_data.get('symbol'),
                        'side': trade_data.get('side'),
                        'quantity': trade_data.get('quantity'),
                        'sl': trade_data.get('sl'),
                        'tp': trade_data.get('tp'),
                        'bars_held': trade_data.get('bars_held', 0),
                        'sl_order_id': trade_data.get('sl_order_id'),
                    }
                logger.info(f"Loaded {len(positions)} open positions from state file")
                return positions
        except Exception as e:
            logger.error(f"Failed to load positions state: {e}")
            return {}
    
    def _get_account_equity(self) -> float:
        """Get current account equity."""
        try:
            balance = self.kraken.get_balance()
            return balance['equity']
        except Exception as e:
            logger.error(f"Failed to get account equity: {str(e)}")
            return 0
    
    def process_webhook(self, payload: Dict) -> Tuple[int, Dict]:
        """
        Process incoming TradingView webhook.
        
        Args:
            payload: Webhook JSON payload
            
        Returns:
            (http_status, response_dict)
        """
        try:
            logger.info(f"Webhook received: {json.dumps(payload)}")
            
            # Parse signal
            is_valid, signal, error_msg = self.signal_parser.parse(payload)
            if not is_valid:
                logger.warning(f"Invalid signal: {error_msg}")
                return 400, {"status": "error", "message": error_msg}
            
            symbol = signal['symbol']
            action = signal['action']
            
            # Handle entry signals
            if action in ['LONG', 'SHORT']:
                return self._handle_entry(signal)
            
            # Handle exit signals
            elif action.startswith('CLOSE_'):
                return self._handle_exit(symbol, action)
            
            else:
                return 400, {"status": "error", "message": f"Unknown action: {action}"}
        
        except Exception as e:
            logger.error(f"Webhook processing error: {str(e)}")
            return 500, {"status": "error", "message": str(e)}
    
    def _handle_entry(self, signal: Dict) -> Tuple[int, Dict]:
        """
        Handle entry signal (LONG or SHORT).
        
        Returns:
            (http_status, response_dict)
        """
        symbol = signal['symbol']
        action = signal['action']
        price = signal['price']
        supertrend = signal['supertrend']
        rsi = signal.get('rsi', 50)
        
        logger.info(f"Processing {action} entry for {symbol}")
        
        # Check if position already open
        try:
            positions = self.kraken.get_open_positions(symbol)
            # Normalize symbol for comparison (CCXT returns ETH/USDT:USDT, bot uses ETHUSDT)
            normalized_symbol = symbol.replace('/', '').replace(':USDT', '')
            if any(normalized_symbol in pos_sym for pos_sym in self.open_positions.keys()):
                msg = f"Position already open for {symbol}"
                logger.warning(msg)
                return 409, {"status": "conflict", "message": msg}
        except Exception as e:
            logger.error(f"Position check failed: {str(e)}")
            return 500, {"status": "error", "message": str(e)}
        
        # Check trading allowed
        equity = self._get_account_equity()
        can_trade, reason = self.risk_manager.can_trade(equity)
        if not can_trade:
            logger.warning(f"Cannot trade: {reason}")
            return 403, {"status": "forbidden", "message": reason}
        
        # Validate entry conditions
        is_valid, msg = self.signal_parser.validate_entry_conditions(signal, price, supertrend, rsi)
        if not is_valid:
            logger.warning(f"Entry conditions not met: {msg}")
            return 400, {"status": "error", "message": msg}
        
        # Calculate position size
        try:
            # SL = supertrend line
            stop_loss = supertrend
            
            # Apply position sizing multiplier if drawdown active
            multiplier = self.risk_manager.get_position_size_multiplier()
            
            # Calculate base position
            pos_calc = self.position_sizer.calculate(
                account_equity=equity,
                entry_price=price,
                stop_loss=stop_loss,
            )
            
            quantity = pos_calc['quantity'] * multiplier
            
            # Calculate TP
            tp = self.position_sizer.calculate_take_profit(price, stop_loss)
            
            logger.info(
                f"Position: {quantity} @ {price}, SL: {stop_loss}, TP: {tp} "
                f"(multiplier: {multiplier})"
            )
            
        except Exception as e:
            logger.error(f"Position sizing error: {str(e)}")
            return 500, {"status": "error", "message": str(e)}
        
        # Place order
        order_side = 'buy' if action == 'LONG' else 'sell'
        
        try:
            success, order, error = self.kraken.place_market_order(
                symbol=symbol,
                side=order_side,
                quantity=quantity,
            )
            
            if not success:
                logger.error(f"Order failed: {error}")
                return 400, {"status": "error", "message": error}
            
            # Record in risk manager
            self.risk_manager.record_trade_entry()
            
            # Store trade in memory
            trade = {
                "symbol": symbol,
                "side": action,
                "entry_price": price,
                "sl": stop_loss,
                "tp": tp,
                "quantity": quantity,
                "entry_time": datetime.utcnow(),
                "order_id": order['order_id'],
            }
            
            self.open_positions[symbol] = trade
            
            # Persist positions to JSON
            self._save_positions()
            
            # Place exchange-level stop loss order on Kraken
            sl_side = 'sell' if action == 'LONG' else 'buy'
            sl_success, sl_order, sl_error = self.kraken.place_stop_loss_order(
                symbol=symbol,
                side=sl_side,
                quantity=quantity,
                stop_price=stop_loss
            )
            
            if sl_success:
                trade['sl_order_id'] = sl_order['sl_order_id']
                self._save_positions()  # Save the SL order ID
                logger.info(f"✓ Exchange SL placed @ {stop_loss}")
            else:
                logger.error(f"⚠️ Exchange SL placement failed: {sl_error}")
                trade['sl_order_id'] = None
                self.alerter.alert_risk_event(f"⚠️ SL Placement Failed: {sl_error}")
                self._save_positions()
            
            # Send alert
            if action == 'LONG':
                self.alerter.alert_entry_long(trade)
            else:
                self.alerter.alert_entry_short(trade)
            
            logger.info(f"✓ Entry executed: {action} {quantity} {symbol} @ {price}")
            
            return 200, {
                "status": "success",
                "action": action,
                "symbol": symbol,
                "quantity": quantity,
                "entry_price": price,
                "sl": stop_loss,
                "tp": tp,
            }
        
        except Exception as e:
            logger.error(f"Entry execution error: {str(e)}")
            return 500, {"status": "error", "message": str(e)}
    
    def _handle_exit(self, symbol: str, action: str) -> Tuple[int, Dict]:
        """
        Handle exit signal (CLOSE_*).
        
        Returns:
            (http_status, response_dict)
        """
        if symbol not in self.open_positions:
            msg = f"No open position for {symbol}"
            logger.warning(msg)
            return 404, {"status": "not_found", "message": msg}
        
        trade = self.open_positions[symbol]
        exit_type = action.replace('CLOSE_', '')
        
        logger.info(f"Processing {exit_type} exit for {symbol}")
        
        try:
            # Cancel exchange SL order if it exists
            if trade.get('sl_order_id'):
                logger.info(f"Cancelling exchange SL order {trade['sl_order_id']}")
                cancel_success, cancel_error = self.kraken.cancel_order(
                    trade['sl_order_id'], symbol
                )
                if not cancel_success:
                    logger.warning(f"Failed to cancel SL: {cancel_error}")
            
            # Close position
            success, order, error = self.kraken.close_position(symbol)
            
            if not success:
                # If Kraken already triggered the SL, position is already closed
                if 'No open position' in error or 'already closed' in error:
                    logger.info(f"Exchange SL already triggered for {symbol} — treating as successful exit")
                    # Use last ticker price as exit price
                    try:
                        success, ticker, ticker_error = self.kraken.get_ticker(symbol)
                        if success:
                            exit_price = ticker.get('last', trade['entry_price'])
                        else:
                            exit_price = trade['entry_price']
                    except:
                        exit_price = trade['entry_price']
                    order = {'close_price': exit_price}
                    success = True
                else:
                    logger.error(f"Close failed: {error}")
                    return 400, {"status": "error", "message": error}
            
            exit_price = order.get('close_price', 0)
            
            # Fallback if price is 0 or None (Kraken may not populate immediately)
            if not exit_price or exit_price == 0:
                try:
                    success, ticker, error = self.kraken.get_ticker(symbol)
                    if success:
                        exit_price = ticker.get('last', trade['entry_price'])
                        logger.warning(f"Exit: fill price unavailable for {symbol}, using last price: {exit_price}")
                    else:
                        logger.error(f"Exit: failed to get ticker: {error}, using entry price")
                        exit_price = trade['entry_price']
                except Exception as e:
                    logger.error(f"Exit: failed to get fallback price: {e}, using entry price")
                    exit_price = trade['entry_price']
            
            # Calculate P&L
            pnl_calc = self.position_sizer.calculate_pnl(
                entry_price=trade['entry_price'],
                exit_price=exit_price,
                quantity=trade['quantity'],
                side=trade['side'],
            )
            
            # Record exit
            trade['exit_price'] = exit_price
            trade['exit_type'] = exit_type
            trade['p&l_usd'] = pnl_calc['pnl_usd']
            trade['p&l_pct'] = pnl_calc['pnl_pct']
            trade['bars_held'] = self._calculate_bars_held(trade)
            
            # Log to CSV
            self.logger.log_trade({
                "timestamp": datetime.utcnow().isoformat(),
                "symbol": symbol,
                "side": trade['side'],
                "entry_price": trade['entry_price'],
                "sl": trade['sl'],
                "tp": trade['tp'],
                "exit_type": exit_type,
                "exit_price": exit_price,
                "p&l_usd": pnl_calc['pnl_usd'],
                "p&l_pct": pnl_calc['pnl_pct'],
                "bars_held": trade.get('bars_held', 0),
            })
            
            # Update risk manager
            equity = self._get_account_equity()
            self.risk_manager.record_trade_exit(pnl_calc['pnl_usd'], equity)
            
            # Send alert
            if exit_type == 'HARDSTOP':
                self.alerter.alert_exit_hardstop(trade)
            elif exit_type == 'SOFTSTOP':
                self.alerter.alert_exit_softstop(trade)
            elif exit_type == 'TAKEPROFIT':
                self.alerter.alert_exit_takeprofit(trade)
            elif exit_type == 'TIMEOUT':
                self.alerter.alert_exit_timeout(trade)
            
            # Remove from open positions
            del self.open_positions[symbol]
            
            # Persist positions to JSON
            self._save_positions()
            
            logger.info(f"✓ Exit executed: {exit_type} {symbol} @ {exit_price}, P&L: ${pnl_calc['pnl_usd']:.2f}")
            
            return 200, {
                "status": "success",
                "action": action,
                "symbol": symbol,
                "exit_price": exit_price,
                "p&l_usd": pnl_calc['pnl_usd'],
                "p&l_pct": pnl_calc['pnl_pct'],
            }
        
        except Exception as e:
            logger.error(f"Exit execution error: {str(e)}")
            return 500, {"status": "error", "message": str(e)}
    
    def _calculate_bars_held(self, trade: Dict) -> int:
        """Calculate number of 4H candles held."""
        entry_time = trade.get('entry_time')
        if not entry_time:
            return 0
        
        duration = datetime.utcnow() - entry_time
        bars = int(duration.total_seconds() / (4 * 3600))  # 4H bars
        return bars


# Initialize bot (global instance)
try:
    bot = TradingBot('config.yaml')
except Exception as e:
    logger.error(f"Failed to initialize bot: {str(e)}")
    bot = None


# Flask routes
@app.route('/webhook', methods=['POST'])
def webhook():
    """TradingView webhook endpoint."""
    if not bot:
        return jsonify({"status": "error", "message": "Bot not initialized"}), 500
    
    try:
        payload = request.get_json()
        if not payload:
            return jsonify({"status": "error", "message": "Invalid JSON"}), 400
        
        status_code, response = bot.process_webhook(payload)
        return jsonify(response), status_code
    
    except Exception as e:
        logger.error(f"Webhook error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint."""
    if not bot:
        return jsonify({"status": "unhealthy"}), 500
    
    try:
        equity = bot._get_account_equity()
        return jsonify({
            "status": "healthy",
            "equity": equity,
            "open_positions": len(bot.open_positions),
            "timestamp": datetime.utcnow().isoformat(),
        }), 200
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


@app.route('/status', methods=['GET'])
def status():
    """Bot status endpoint."""
    if not bot:
        return jsonify({"status": "error"}), 500
    
    try:
        equity = bot._get_account_equity()
        risk_status = bot.risk_manager.get_status()
        trade_stats = bot.logger.get_stats()
        
        return jsonify({
            "status": "ok",
            "equity": equity,
            "risk": risk_status,
            "trades": trade_stats,
            "open_positions": list(bot.open_positions.keys()),
        }), 200
    
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)}), 500


def signal_handler(sig, frame):
    """Graceful shutdown on SIGINT/SIGTERM."""
    logger.info("Shutting down gracefully...")
    sys.exit(0)


if __name__ == '__main__':
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=" * 60)
    logger.info("Proppa Kraken Crypto Bot Starting")
    logger.info("=" * 60)
    
    # Run Flask (development; use Gunicorn for production)
    app.run(
        host='0.0.0.0',
        port=5000,
        debug=False,
        use_reloader=False,
    )
