import asyncio
import logging
import json
import threading
from datetime import datetime
import pandas as pd
import numpy as np
from sqlalchemy.orm import scoped_session
from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient
from binance.spot import Spot
from binance.error import ClientError
from database import DatabaseManager, ActiveTrade, TradeHistory, Settings

# Autore: Emanuele Tolomei
# Versione: Mark V2.2 (Full Logic + Wallet Sync + Hard Budget)

def round_step_size(quantity, step_size):
    """Rounds quantity to the correct step size precision."""
    if step_size == 0: return quantity
    precision = int(round(-np.log10(step_size), 0))
    return float(f"{quantity:.{precision}f}")

class MarkBot:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret

        # Validate API Key format to prevent silent 401 errors
        if not api_key or len(api_key) < 64 or ' ' in api_key:
             print("❌ CRITICAL: Invalid BINANCE_API_KEY format detected in .env!")

        # Initialize Database connection
        self.db_manager = DatabaseManager()
        self.Session = scoped_session(self.db_manager.Session)
        
        # Initialize Binance Client
        # show_limit_usage=False prevents excessive logging of headers
        self.client = Spot(api_key=api_key, api_secret=api_secret, show_limit_usage=False)
        self.ws_client = None

        # Configuration & State
        self.whitelist = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'ADA', 'DOGE']
        self.quote_currency = 'EUR'
        self.klines_cache = {}
        self.exchange_info = {}
        self.running = False
        self.loop = None
        self.status_message = "Initializing..."
        self.logs = []
        self.cache_lock = threading.Lock()

        # Default Strategy Settings (Overwritten by DB)
        self.base_order_size = 20.0  # Safe default
        self.max_safety_orders = 3
        self.dca_volume_scale = 1.5
        self.dca_step_scale = 0.02
        self.tp_percent = 0.015
        self.safety_sl_percent = 0.10
        self.max_budget = 525.0 # Set to match User Equity

        # Load dynamic settings from DB
        self.load_settings()
        # Fetch symbol rules (step size, min notional)
        self.update_exchange_info()

    def log(self, message):
        """Thread-safe logging to internal buffer and stdout."""
        ts = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {message}"
        # Keep only last 100 logs in RAM for Dashboard
        self.logs.insert(0, msg)
        if len(self.logs) > 100: self.logs.pop()
        print(msg)

    def load_settings(self):
        """Loads trading parameters from SQLite settings table."""
        try:
            self.base_order_size = self.db_manager.get_setting('base_order_size', 20.0, float)
            self.max_safety_orders = self.db_manager.get_setting('max_safety_orders', 3, int)
            self.dca_volume_scale = self.db_manager.get_setting('dca_volume_scale', 1.5, float)
            self.dca_step_scale = self.db_manager.get_setting('dca_step_scale', 0.02, float)
            self.tp_percent = self.db_manager.get_setting('tp_percent', 0.015, float)
            self.max_budget = self.db_manager.get_setting('max_budget', 525.0, float)
        except Exception as e:
            self.log(f"Error loading settings: {e}")

    def update_exchange_info(self):
        """Fetches trading rules (Lot Size, Min Notional) from Binance."""
        try:
            info = self.client.exchange_info()
            for s in info['symbols']:
                if s['symbol'].endswith(self.quote_currency):
                    filters = {f['filterType']: f for f in s['filters']}
                    lot_size = filters.get('LOT_SIZE')
                    min_notional = filters.get('NOTIONAL') or filters.get('MIN_NOTIONAL')
                    self.exchange_info[s['symbol']] = {
                        'step_size': float(lot_size['stepSize']) if lot_size else 0.0,
                        'min_notional': float(min_notional['minNotional']) if min_notional else 5.5
                    }
        except Exception as e:
            self.log(f"ExInfo Error: {e}")

    # --- WALLET SYNC LOGIC (The "Ghost Hunter") ---
    def sync_wallet_positions(self):
        """
        Scans Binance Wallet for whitelisted assets not in DB and adopts them.
        CRITICAL: This allows finding 'orphaned' assets like BNB dust or manual buys.
        """
        self.log("🔄 Syncing Wallet...")
        try:
            # 1. Fetch Real Balances from Binance
            acc = self.client.account()
            # Map asset -> total qty (free + locked)
            balances = {b['asset']: float(b['free']) + float(b['locked']) for b in acc['balances']}
            
            session = self.Session()
            count = 0
            
            for coin in self.whitelist:
                symbol = f"{coin}{self.quote_currency}"
                qty = balances.get(coin, 0.0)
                
                # Check if this asset is already tracked in our DB
                db_trade = session.query(ActiveTrade).filter_by(symbol=symbol).first()
                
                # If NOT in DB but we have balance in Wallet -> Orphan Found!
                if not db_trade and qty > 0:
                    try:
                        # Get current price to estimate value
                        current_price = float(self.client.ticker_price(symbol)['price'])
                    except:
                        continue # Skip if API fails for this symbol
                        
                    val_eur = qty * current_price
                    
                    # Threshold: Ignore dust < 2 EUR to avoid clutter
                    if val_eur > 2.0:
                        self.log(f"♻️ Found orphan {symbol} ({qty:.4f} units, €{val_eur:.2f}). Adopting...")
                        
                        # Create new trade record.
                        # Since we don't know original entry, we use CURRENT PRICE.
                        # This resets PnL to 0% for this specific asset.
                        new_trade = ActiveTrade(
                            symbol=symbol,
                            entry_price=current_price,
                            quantity=qty,
                            current_price=current_price,
                            safety_order_count=0,
                            base_order_price=current_price,
                            total_cost=val_eur,
                            next_safety_order_price=current_price * (1 - self.dca_step_scale)
                        )
                        session.add(new_trade)
                        count += 1
            
            session.commit()
            if count > 0: self.log(f"✅ Sync Complete: Adopted {count} positions.")
            else: self.log("✅ Sync Complete: No new orphans found.")
            
        except Exception as e:
            self.log(f"❌ Wallet Sync Error: {e}")
        finally:
            session.close()

    # --- START / STOP & ASYNC LOOP ---
    def start(self):
        if self.running: return
        self.running = True
        self.log("Mark V2 Starting...")
        
        # 1. RUN SYNC ON START
        # This ensures we see BNB or any other asset before trading starts
        try:
            self.sync_wallet_positions()
        except Exception as e:
            self.log(f"Sync Crash Prevention: {e}")

        # 2. START WEBSOCKETS
        # We use a combined stream for efficiency
        self.ws_client = SpotWebsocketStreamClient(on_message=self.on_kline_message, is_combined=True)
        streams = []
        for coin in self.whitelist:
            symbol = f"{coin}{self.quote_currency}".lower() # WS requires lowercase
            streams.append(f"{symbol}@kline_1m")
            
        self.ws_client.stream(streams)
            
        # 3. START ASYNC LOGIC THREAD
        threading.Thread(target=self._run_async_loop, daemon=True).start()

    def stop(self):
        self.running = False
        if self.ws_client: self.ws_client.stop()
        self.log("Mark V2 Stopped.")

    def _run_async_loop(self):
        """Sets up the asyncio loop in a separate thread."""
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.main_loop())

    async def main_loop(self):
        """The heartbeat of the bot."""
        while self.running:
            try:
                self.check_strategies()
                self.status_message = "Running - Monitoring Markets"
                await asyncio.sleep(1) # Check every 1 second
            except Exception as e:
                self.log(f"Loop Error: {e}")
                await asyncio.sleep(5) # Pause on error

    def on_kline_message(self, _, msg):
        """Callback for WebSocket messages."""
        try:
            data = None
            if isinstance(msg, str): msg = json.loads(msg)
            if 'data' in msg: data = msg['data']
            elif 'k' in msg: data = msg
            
            if data and 'k' in data:
                k = data['k']
                # Symbol comes as 'btceur' in combined stream, we need 'BTCEUR'
                symbol = data['s'] 
                self.update_kline_cache(symbol, k)
        except: pass

    def update_kline_cache(self, symbol, kline):
        """Updates internal DataFrame with new candle data."""
        with self.cache_lock:
            data = {
                'ts': pd.to_datetime(kline['t'], unit='ms'),
                'open': float(kline['o']), 'high': float(kline['h']), 'low': float(kline['l']),
                'close': float(kline['c']), 'volume': float(kline['v'])
            }
            if symbol not in self.klines_cache:
                self.klines_cache[symbol] = pd.DataFrame([data])
            else:
                df = self.klines_cache[symbol]
                if df.iloc[-1]['ts'] == data['ts']: 
                    # Update current candle (it's still open)
                    df.iloc[-1] = data
                else:
                    # New candle closed, append
                    df = pd.concat([df, pd.DataFrame([data])], ignore_index=True)
                    # Keep memory usage low (last 100 candles max)
                    if len(df) > 100: df = df.iloc[-100:]
                self.klines_cache[symbol] = df

    # --- TECHNICAL ANALYSIS ---
    def calculate_indicators(self, symbol):
        with self.cache_lock:
            df = self.klines_cache.get(symbol)
            if df is None or len(df) < 20: return None
            df = df.copy()
        
        # Bollinger Bands (20, 2)
        df['ma20'] = df['close'].rolling(window=20).mean()
        df['std20'] = df['close'].rolling(window=20).std()
        df['lower_bb'] = df['ma20'] - (2.0 * df['std20'])
        
        # RSI 14
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 0.000001)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        return df.iloc[-1]

    # --- STRATEGY CORE ---
    def check_strategies(self):
        session = self.Session()
        try:
            active_trades = {t.symbol: t for t in session.query(ActiveTrade).all()}
            for coin in self.whitelist:
                symbol = f"{coin}{self.quote_currency}"
                last_candle = self.calculate_indicators(symbol)
                if last_candle is None: continue
                
                current_price = last_candle['close']
                
                # Check if we are already in a trade
                if symbol in active_trades:
                    self.manage_active_trade(session, active_trades[symbol], current_price)
                else:
                    self.scan_for_entry(symbol, last_candle)
            session.commit()
        except Exception as e:
            session.rollback()
            self.log(f"Strategy Error: {e}")
        finally:
            session.close()

    def scan_for_entry(self, symbol, candle):
        """Looks for Mean Reversion entry signals."""
        if pd.isna(candle['rsi']) or pd.isna(candle['lower_bb']): return
        
        # LOGIC: Oversold (RSI < 30) AND Price below Lower Bollinger Band
        if candle['rsi'] < 30 and candle['close'] < candle['lower_bb']:
            self.execute_buy(symbol, self.base_order_size, "Initial Entry")

    def manage_active_trade(self, session, trade, current_price):
        """Manages DCA and Take Profit for open trades."""
        trade.current_price = current_price
        if current_price > trade.highest_price: trade.highest_price = current_price

        # 1. TAKE PROFIT CHECK
        if current_price >= trade.entry_price * (1 + self.tp_percent):
            self.execute_sell(trade, current_price, "Take Profit")
            return

        # 2. DCA SAFETY ORDER CHECK
        # Initialize next target if missing
        if trade.next_safety_order_price == 0:
             trade.next_safety_order_price = trade.entry_price * (1 - self.dca_step_scale)

        if trade.safety_order_count < self.max_safety_orders:
            if current_price <= trade.next_safety_order_price:
                self.execute_safety_order(trade, current_price)
                return

        # 3. HARD STOP LOSS (Emergency Only)
        # Only sell if price crashes 10% below average entry
        if current_price <= trade.entry_price * (1 - self.safety_sl_percent):
            self.execute_sell(trade, current_price, "Hard Stop Loss")

    # --- BUDGET & EXECUTION ---
    def check_budget(self, session, amount):
        """HARD BUDGET CAP: Prevents bot from spending more than allowed."""
        current_invested = sum([t.total_cost for t in session.query(ActiveTrade).all()])
        remaining = self.max_budget - current_invested
        
        if amount > remaining:
            self.log(f"⛔ Budget Limit Reached! Req: {amount:.2f}€, Rem: {remaining:.2f}€")
            return False
        return True

    def execute_buy(self, symbol, amount_eur, reason):
        session = self.Session()
        # Check Budget first
        if not self.check_budget(session, amount_eur): 
            session.close()
            return

        try:
            info = self.exchange_info.get(symbol, {'step_size': 0.00001, 'min_notional': 5.0})
            price = self.get_current_price_fast(symbol)
            if price == 0: return

            quantity = round_step_size(amount_eur / price, info['step_size'])
            
            # Min Notional Check
            if (quantity * price) < info['min_notional']: return

            self.log(f"🛒 BUY {symbol} ({amount_eur}€) - {reason}")
            
            # PLACE ORDER ON BINANCE
            order = self.client.new_order(symbol=symbol, side='BUY', type='MARKET', quoteOrderQty=amount_eur)
            
            filled_qty = float(order['executedQty'])
            spent = float(order['cummulativeQuoteQty'])
            avg_price = spent / filled_qty
            
            # RECORD IN DB
            trade = ActiveTrade(
                symbol=symbol, entry_price=avg_price, quantity=filled_qty,
                current_price=avg_price, safety_order_count=0,
                base_order_price=avg_price, total_cost=spent,
                next_safety_order_price=avg_price * (1 - self.dca_step_scale)
            )
            session.add(trade)
            session.commit()
            self.log(f"✅ Position Opened {symbol}")

        except Exception as e:
            self.log(f"❌ Buy Failed {symbol}: {e}")
        finally:
            session.close()

    def execute_safety_order(self, trade, current_price):
        symbol = trade.symbol
        next_count = trade.safety_order_count + 1
        # Martingale Sizing: Base * (1.5 ^ Count)
        size = self.base_order_size * (self.dca_volume_scale ** next_count)
        
        session = self.Session.object_session(trade)
        if not self.check_budget(session, size): return

        self.log(f"🛡️ Safety Order #{next_count} {symbol} ({size:.2f}€)")
        try:
            order = self.client.new_order(symbol=symbol, side='BUY', type='MARKET', quoteOrderQty=size)
            filled = float(order['executedQty'])
            spent = float(order['cummulativeQuoteQty'])
            
            # Update Trade Logic
            trade.quantity += filled
            trade.total_cost += spent
            trade.entry_price = trade.total_cost / trade.quantity # New Weighted Average
            trade.safety_order_count = next_count
            trade.next_safety_order_price = trade.entry_price * (1 - self.dca_step_scale)
            
            self.log(f"✅ DCA Success {symbol}. New Avg: {trade.entry_price:.4f}")
        except Exception as e:
            self.log(f"❌ DCA Failed {symbol}: {e}")

    def execute_sell(self, trade, price, reason):
        symbol = trade.symbol
        qty = round_step_size(trade.quantity, self.exchange_info.get(symbol, {}).get('step_size', 0.00001))
        
        try:
            self.log(f"📉 SELLING {symbol} ({reason})...")
            order = self.client.new_order(symbol=symbol, side='SELL', type='MARKET', quantity=qty)
            
            received = float(order['cummulativeQuoteQty'])
            pnl = received - trade.total_cost
            pct = (pnl / trade.total_cost) * 100
            
            session = self.Session()
            # Remove from Active
            session.query(ActiveTrade).filter_by(symbol=symbol).delete()
            # Add to History
            session.add(TradeHistory(
                symbol=symbol, entry_price=trade.entry_price, exit_price=price,
                quantity=qty, pnl_abs=pnl, pnl_pct=pct, reason=reason, close_time=datetime.utcnow()
            ))
            session.commit()
            self.log(f"💰 SOLD {symbol} | PnL: {pnl:.2f}€")
            session.close()

        except Exception as e:
            self.log(f"❌ Sell Failed {symbol}: {e}")
            if "insufficient balance" in str(e).lower(): self.handle_dust_error(trade)

    def handle_dust_error(self, trade):
        session = self.Session()
        t = session.query(ActiveTrade).filter_by(symbol=trade.symbol).first()
        if t: t.is_dust = True
        session.commit()
        session.close()
        self.log(f"⚠️ Marked {trade.symbol} as Dust (Too small to sell)")

    def force_close(self, symbol):
        session = self.Session()
        t = session.query(ActiveTrade).filter_by(symbol=symbol).first()
        if t: self.execute_sell(t, self.get_current_price_fast(symbol), "Force Close")
        session.close()

    def forget_trade(self, symbol):
        """Removes trade from DB without selling on Binance."""
        session = self.Session()
        session.query(ActiveTrade).filter_by(symbol=symbol).delete()
        session.commit()
        session.close()
        self.log(f"🗑️ Forgot {symbol} (DB Only)")

    def sweep_dust(self):
        try:
            session = self.Session()
            dust = session.query(ActiveTrade).filter_by(is_dust=True).all()
            assets = [t.symbol.replace(self.quote_currency, '') for t in dust]
            if assets:
                self.client.transfer_dust(asset=assets)
                for t in dust: session.delete(t)
                session.commit()
                self.log(f"🧹 Swept Dust: {assets}")
            session.close()
        except Exception as e: self.log(f"Sweep Error: {e}")

    def get_current_price_fast(self, symbol):
        with self.cache_lock:
            if symbol in self.klines_cache: return self.klines_cache[symbol].iloc[-1]['close']
        try: return float(self.client.ticker_price(symbol)['price'])
        except: return 0.0
