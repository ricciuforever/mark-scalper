import asyncio
import logging
import json
import threading
import time
from datetime import datetime
import pandas as pd
import numpy as np
from sqlalchemy.orm import scoped_session
from binance.websocket.spot.websocket_stream import SpotWebsocketStreamClient
from binance.spot import Spot
from binance.error import ClientError
from database import DatabaseManager, ActiveTrade, TradeHistory, Settings

# Helper to round quantities
def round_step_size(quantity, step_size):
    if step_size == 0: return quantity
    # Use string formatting to avoid float precision issues
    precision = int(round(-np.log10(step_size), 0))
    return float(f"{quantity:.{precision}f}")

class MarkBot:
    def __init__(self, api_key, api_secret):
        self.api_key = api_key
        self.api_secret = api_secret
        
        # Validate API Key format
        if not api_key or len(api_key) < 64 or ' ' in api_key:
             print("❌ CRITICAL: Invalid BINANCE_API_KEY format detected in .env!")
        
        # Database
        self.db_manager = DatabaseManager()
        self.Session = scoped_session(self.db_manager.Session)

        # Binance Clients
        self.client = Spot(api_key=api_key, api_secret=api_secret)
        self.ws_client = None 
        
        # State
        self.whitelist = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'ADA', 'DOGE']
        self.quote_currency = 'EUR'
        self.klines_cache = {} 
        self.exchange_info = {}
        self.running = False
        self.loop = None
        self.status_message = "Initializing..."
        self.logs = []
        self.cache_lock = threading.Lock()
        self.so_retry_cooldown = {} # {symbol: timestamp}
        
        # Strategy Config
        self.base_order_size = 50.0
        self.max_safety_orders = 3
        self.dca_volume_scale = 1.5
        self.dca_step_scale = 0.02 # 2%
        self.tp_percent = 0.015 # 1.5%
        self.safety_sl_percent = 0.10 # 10%
        self.max_budget = 500.0 # Default budget cap
        
        self.load_settings()
        self.update_exchange_info()

    def preload_history(self):
        self.log("📥 Preloading Market Data...")
        for coin in self.whitelist:
            symbol = f"{coin}{self.quote_currency}"
            try:
                # Fetch last 30 candles (need 20 for BB/MA)
                klines = self.client.klines(symbol=symbol, interval='1m', limit=30)

                data_list = []
                for k in klines:
                    data_list.append({
                        'ts': pd.to_datetime(k[0], unit='ms'),
                        'open': float(k[1]),
                        'high': float(k[2]),
                        'low': float(k[3]),
                        'close': float(k[4]),
                        'volume': float(k[5])
                    })

                df = pd.DataFrame(data_list)
                with self.cache_lock:
                    self.klines_cache[symbol] = df

            except Exception as e:
                self.log(f"⚠️ Preload Failed for {symbol}: {e}")

    def log(self, message):
        ts = datetime.now().strftime("%H:%M:%S")
        msg = f"[{ts}] {message}"
        self.logs.insert(0, msg)
        if len(self.logs) > 100: self.logs.pop()
        print(msg)

    def load_settings(self):
        try:
            self.base_order_size = self.db_manager.get_setting('base_order_size', 50.0, float)
            self.max_safety_orders = self.db_manager.get_setting('max_safety_orders', 3, int)
            self.dca_volume_scale = self.db_manager.get_setting('dca_volume_scale', 1.5, float)
            self.dca_step_scale = self.db_manager.get_setting('dca_step_scale', 0.02, float)
            self.tp_percent = self.db_manager.get_setting('tp_percent', 0.015, float)
            self.safety_sl_percent = self.db_manager.get_setting('safety_sl_percent', 0.10, float)
            self.max_budget = self.db_manager.get_setting('max_budget', 500.0, float)
        except Exception as e:
            self.log(f"Error loading settings: {e}")

    def update_exchange_info(self):
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

    # --- WebSocket Handling ---
    def start(self):
        if self.running: return
        
        # 0. Sync Wallet positions (Adopt orphans)
        try:
            self.sync_wallet_positions()
        except Exception as e:
            self.log(f"Sync Error: {e}")

        self.running = True
        self.log("Mark V2 Starting...")
        
        # Preload data to avoid waiting 20 mins
        self.preload_history()

        self.ws_client = SpotWebsocketStreamClient(
            on_message=self.on_kline_message,
            is_combined=True
        )
        
        for coin in self.whitelist:
            symbol = f"{coin}{self.quote_currency}"
            self.ws_client.kline(symbol=symbol, interval='1m')
        
        threading.Thread(target=self._run_async_loop, daemon=True).start()

    def sync_wallet_positions(self):
        """Checks Binance wallet for assets that are not in DB and adopts them."""
        self.log("🔄 Syncing Wallet...")
        try:
            # Get real balances
            acc = self.client.account()
            balances = {b['asset']: float(b['free']) + float(b['locked']) for b in acc['balances']}
            
            session = self.Session()
            active_symbols = [t.symbol for t in session.query(ActiveTrade).all()]
            
            for coin in self.whitelist:
                symbol = f"{coin}{self.quote_currency}"
                qty = balances.get(coin, 0.0)
                
                # Check value > 1 EUR (approx) to skip dust
                # We need a price estimation.
                price = self.get_current_price_fast(symbol)
                if price == 0:
                    # Fallback to ticker
                    try:
                        price = float(self.client.ticker_price(symbol)['price'])
                    except:
                        continue
                
                value = qty * price
                
                # If significant value found AND not in DB
                if value > 1.0 and symbol not in active_symbols:
                    self.log(f"♻️ Found orphan position in wallet: {symbol} ({qty:.4f} | {value:.2f}€). Adopting.")
                    
                    # Create Active Trade
                    trade = ActiveTrade(
                        symbol=symbol,
                        entry_price=price, # Use current price as entry
                        quantity=qty,
                        current_price=price,
                        safety_order_count=0,
                        highest_price=price,
                        base_order_price=price,
                        total_cost=value,
                        next_safety_order_price=price * (1 - self.dca_step_scale),
                        is_dust=False
                    )
                    session.add(trade)
                    session.commit()
            
            session.close()
        except Exception as e:
            self.log(f"Wallet Sync Failed: {e}")
            if 'session' in locals(): session.close()

    def stop(self):
        self.running = False
        if self.ws_client:
            self.ws_client.stop()
        self.log("Mark V2 Stopped.")

    def _run_async_loop(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self.main_loop())

    async def main_loop(self):
        while self.running:
            try:
                self.check_strategies()
                self.status_message = "Running - Monitoring Markets"
                await asyncio.sleep(1)
            except Exception as e:
                self.log(f"Loop Error: {e}")
                await asyncio.sleep(5)

    def on_kline_message(self, _, msg):
        try:
            data = None
            if isinstance(msg, str):
                msg = json.loads(msg)
                
            if 'data' in msg:
                data = msg['data']
            elif 'k' in msg:
                data = msg

            if data and 'k' in data:
                k = data['k']
                symbol = data['s']
                self.update_kline_cache(symbol, k)
                
        except Exception as e:
            pass

    def update_kline_cache(self, symbol, kline):
        with self.cache_lock:
            data = {
                'ts': pd.to_datetime(kline['t'], unit='ms'),
                'open': float(kline['o']),
                'high': float(kline['h']),
                'low': float(kline['l']),
                'close': float(kline['c']),
                'volume': float(kline['v'])
            }
            
            if symbol not in self.klines_cache:
                self.klines_cache[symbol] = pd.DataFrame([data])
            else:
                df = self.klines_cache[symbol]
                if df.iloc[-1]['ts'] == data['ts']:
                    df.iloc[-1] = data
                else:
                    df = pd.concat([df, pd.DataFrame([data])], ignore_index=True)
                    if len(df) > 100:
                        df = df.iloc[-100:]
                self.klines_cache[symbol] = df

    # --- Indicators ---
    def calculate_indicators(self, symbol):
        with self.cache_lock:
            df = self.klines_cache.get(symbol)
            if df is None or len(df) < 20: return None
            df = df.copy()
        
        df['ma20'] = df['close'].rolling(window=20).mean()
        df['std20'] = df['close'].rolling(window=20).std()
        df['upper_bb'] = df['ma20'] + (2.0 * df['std20'])
        df['lower_bb'] = df['ma20'] - (2.0 * df['std20'])
        
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(window=14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(window=14).mean()
        rs = gain / loss.replace(0, 0.000001)
        df['rsi'] = 100 - (100 / (1 + rs))
        
        return df.iloc[-1]

    # --- STRATEGY ENGINE ---
    def check_strategies(self):
        session = self.Session()
        try:
            active_trades = {t.symbol: t for t in session.query(ActiveTrade).all()}
            
            for coin in self.whitelist:
                symbol = f"{coin}{self.quote_currency}"
                
                # Check Indicators
                last_candle = self.calculate_indicators(symbol)
                if last_candle is None: continue
                
                current_price = last_candle['close']
                
                # Update Active Trade status
                if symbol in active_trades:
                    trade = active_trades[symbol]
                    self.manage_active_trade(session, trade, current_price)
                else:
                    self.scan_for_entry(symbol, last_candle, session)
            
            session.commit()
        except Exception as e:
            session.rollback()
            self.log(f"Strategy Error: {e}")
        finally:
            session.close()

    def scan_for_entry(self, symbol, candle, session=None):
        # Entry Condition: RSI < 30 AND Price < Lower BB
        if pd.isna(candle['rsi']) or pd.isna(candle['lower_bb']): return
        
        if candle['rsi'] < 30 and candle['close'] < candle['lower_bb']:
            self.execute_buy(symbol, self.base_order_size, "Initial Entry", session)

    def manage_active_trade(self, session, trade, current_price):
        # Update current price in DB
        trade.current_price = current_price
        if current_price > trade.highest_price:
            trade.highest_price = current_price
            
        # 1. Take Profit
        # Target: Avg Entry * (1 + TP%)
        # Note: In V2 we use simple Fixed TP from avg price.
        # Future: Could add trailing here too.
        
        avg_price = trade.entry_price # This is the WEIGHTED AVERAGE
        tp_price = avg_price * (1 + self.tp_percent)
        
        if current_price >= tp_price:
            self.execute_sell(trade, current_price, "Take Profit", session)
            return

        # 2. Safety Orders (DCA)
        # Trigger: Price < Next Safety Order Price
        
        # Calculate Next SO Price if not set (e.g. fresh migration)
        if trade.next_safety_order_price == 0:
             # Default fallback: 2% below avg
             trade.next_safety_order_price = avg_price * (1 - self.dca_step_scale)

        if trade.safety_order_count < self.max_safety_orders:
            if current_price <= trade.next_safety_order_price:
                self.execute_safety_order(trade, current_price, session)
                return
        
        # 3. Hard Stop Loss
        # Only if price drops significantly below (e.g. 10%) total avg price
        stop_price = avg_price * (1 - self.safety_sl_percent)
        if current_price <= stop_price:
            self.execute_sell(trade, current_price, "Hard Stop Loss", session)
            return

    def check_budget(self, required_amount, session=None):
        """Checks if spending 'required_amount' exceeds max_budget."""
        close_session = False
        if session is None:
            session = self.Session()
            close_session = True
            
        try:
            active = session.query(ActiveTrade).all()
            current_invested = sum([t.total_cost for t in active])
            remaining = self.max_budget - current_invested
            
            if required_amount > remaining:
                self.log(f"⛔ Order Skipped: Budget Limit Reached (Req: €{required_amount:.2f}, Rem: €{remaining:.2f})")
                return False
            return True
        finally:
            if close_session:
                session.close()

    def execute_buy(self, symbol, amount_eur, reason, session=None):
        # 0. Check Budget (Strategy Cap)
        if not self.check_budget(amount_eur, session):
            return

        # 0b. Check Wallet Balance (Physical Cap)
        available = self.get_wallet_balance(self.quote_currency)
        if available < amount_eur:
             self.log(f"⛔ Buy Skipped: Insufficient Wallet Funds (Req: {amount_eur:.2f}€, Avail: {available:.2f}€)")
             return

        # 1. Get step size
        info = self.exchange_info.get(symbol, {'step_size': 0.00001, 'min_notional': 5.0})
        
        # 2. Calculate Quantity
        price = self.get_current_price_fast(symbol)
        if price == 0: return

        quantity = amount_eur / price
        quantity = round_step_size(quantity, info['step_size'])
        
        if (quantity * price) < info['min_notional']:
            self.log(f"⚠️ Order too small for {symbol}: {amount_eur}€")
            return

        try:
            amount_eur = round(amount_eur, 2)
            self.log(f"🛒 BUY {symbol} ({amount_eur}€) - {reason}")
            # Real Order
            order = self.client.new_order(symbol=symbol, side='BUY', type='MARKET', quoteOrderQty=amount_eur)
            
            # Parse result
            filled_qty = float(order['executedQty'])
            spent_eur = float(order['cummulativeQuoteQty'])
            avg_fill_price = spent_eur / filled_qty
            
            # Save to DB
            close_session = False
            if session is None:
                session = self.Session()
                close_session = True
                
            try:
                trade = ActiveTrade(
                    symbol=symbol,
                    entry_price=avg_fill_price,
                    quantity=filled_qty,
                    current_price=avg_fill_price,
                    safety_order_count=0,
                    highest_price=avg_fill_price,
                    base_order_price=avg_fill_price,
                    total_cost=spent_eur,
                    next_safety_order_price=avg_fill_price * (1 - self.dca_step_scale)
                )
                session.add(trade)
                # If we own the session, we commit. If not, the caller commits?
                # Actually, for trade execution, we often want immediate commit to avoid race conditions?
                # But if we commit here, we might break the transaction of the caller if it wanted atomicity?
                # In this bot, we treat each order as atomic.
                session.commit()
                self.log(f"✅ Position Opened {symbol}")
            except Exception as db_e:
                session.rollback()
                self.log(f"DB Error on Buy: {db_e}")
            finally:
                if close_session:
                    session.close()
                
        except Exception as e:
            self.log(f"❌ Buy Failed {symbol}: {e}")

    def execute_safety_order(self, trade, current_price, session=None):
        symbol = trade.symbol
        
        # Check Cooldown (prevent log spam)
        last_attempt = self.so_retry_cooldown.get(symbol, 0)
        if time.time() - last_attempt < 60:
            return

        # Calculate Size
        next_count = trade.safety_order_count + 1
        size_eur = self.base_order_size * (self.dca_volume_scale ** next_count)
        
        # Check Budget (Strategy Cap)
        if not self.check_budget(size_eur, session):
            return

        # Check Wallet Balance (Physical Cap)
        available = self.get_wallet_balance(self.quote_currency)
        info = self.exchange_info.get(symbol, {'step_size': 0.00001, 'min_notional': 5.0})

        # Logic: If insufficient but close (>90%), use all available
        if available < size_eur:
            # Check if we can salvage the trade
            if available > (size_eur * 0.90) and available > (info['min_notional'] * 1.1):
                self.log(f"⚠️ Insufficient funds for full SO. Adjusting {size_eur:.2f}€ -> {available:.2f}€ (Using Remainder)")
                size_eur = available * 0.99 # Leave 1% buffer
            else:
                self.log(f"⛔ SO Skipped: Insufficient Wallet Funds (Req: {size_eur:.2f}€, Avail: {available:.2f}€)")
                self.so_retry_cooldown[symbol] = time.time()
                return

        self.log(f"🛡️ Safety Order #{next_count} for {symbol} ({size_eur:.2f}€)")
        
        quantity = size_eur / current_price
        quantity = round_step_size(quantity, info['step_size'])
        
        try:
            size_eur = round(size_eur, 2)
            order = self.client.new_order(symbol=symbol, side='BUY', type='MARKET', quoteOrderQty=size_eur)
            
            filled_qty = float(order['executedQty'])
            spent_eur = float(order['cummulativeQuoteQty'])
            
            # Update Trade in DB
            # Ensure trade is attached to current session if it was passed
            if session and trade not in session:
                trade = session.merge(trade)

            trade.quantity += filled_qty
            trade.total_cost += spent_eur
            
            # New Weighted Average Price
            trade.entry_price = trade.total_cost / trade.quantity
            trade.safety_order_count = next_count
            
            # Update Next Target
            trade.next_safety_order_price = trade.entry_price * (1 - self.dca_step_scale)
            
            if session:
                session.commit()
            
            self.log(f"✅ DCA Executed {symbol}. New Avg: {trade.entry_price:.4f}")
            # Clear cooldown on success
            if symbol in self.so_retry_cooldown:
                del self.so_retry_cooldown[symbol]
            
        except Exception as e:
            self.log(f"❌ DCA Failed {symbol}: {e}")
            # Set cooldown on API failure too
            self.so_retry_cooldown[symbol] = time.time()

    def execute_sell(self, trade, price, reason, session=None):
        symbol = trade.symbol
        qty = trade.quantity
        
        info = self.exchange_info.get(symbol, {'step_size': 0.00001})
        qty_to_sell = round_step_size(qty, info['step_size'])
        
        try:
            self.log(f"📉 SELLING {symbol} ({reason})...")
            
            # Order
            order = self.client.new_order(symbol=symbol, side='SELL', type='MARKET', quantity=qty_to_sell)
            
            total_received = float(order['cummulativeQuoteQty'])
            exit_price = total_received / float(order['executedQty'])
            
            # Calculate PnL
            # Fee is approx 0.1% on sell (deducted from received EUR usually)
            # Net PnL = Received - Cost - (Cost*0.001 [Buy Fee]) - (Received*0.001 [Sell Fee])
            # Simplified:
            
            gross_pnl = total_received - trade.total_cost
            fees = (trade.total_cost * 0.001) + (total_received * 0.001)
            net_pnl = gross_pnl - fees
            pnl_pct = (net_pnl / trade.total_cost) * 100
            
            # Archive
            close_session = False
            if session is None:
                session = self.Session()
                close_session = True

            try:
                # Delete active
                # If trade is not attached, query it again or merge?
                # If we have session, we can just delete trade if attached?
                if trade not in session:
                    # session.delete expects an attached instance or we can query
                    session.query(ActiveTrade).filter_by(symbol=symbol).delete()
                else:
                    session.delete(trade)
                
                # Add history
                hist = TradeHistory(
                    symbol=symbol,
                    entry_price=trade.entry_price,
                    exit_price=exit_price,
                    quantity=qty_to_sell,
                    pnl_abs=net_pnl,
                    pnl_pct=pnl_pct,
                    reason=reason,
                    close_time=datetime.utcnow()
                )
                session.add(hist)
                session.commit()
                self.log(f"💰 SOLD {symbol} | PnL: {net_pnl:.2f}€ ({pnl_pct:.2f}%)")
            except Exception as e:
                self.log(f"DB Error Archive: {e}")
                session.rollback()
            finally:
                if close_session:
                    session.close()

        except Exception as e:
            self.log(f"❌ Sell Failed {symbol}: {e}")
            # Check for dust
            if "Account has insufficient balance" in str(e) or "Filter failure: LOT_SIZE" in str(e):
                self.handle_dust_error(trade, session)

    def handle_dust_error(self, trade, session=None):
        # Mark as dust or remove if value is tiny
        val = trade.quantity * trade.current_price
        if val < 5.0:
            close_session = False
            if session is None:
                session = self.Session()
                close_session = True
                
            try:
                # Re-query if necessary or use merge
                if trade not in session:
                    t = session.query(ActiveTrade).filter_by(symbol=trade.symbol).first()
                else:
                    t = trade
                    
                if t:
                    t.is_dust = True
                    t.last_error = "Dust/MinNotional"
                session.commit()
                self.log(f"⚠️ Marked {trade.symbol} as Dust")
            finally:
                if close_session:
                    session.close()

    def force_close(self, symbol):
        # Manually sell
        session = self.Session()
        trade = session.query(ActiveTrade).filter_by(symbol=symbol).first()
        if trade:
            current_price = self.get_current_price_fast(symbol)
            # Use the local session
            self.execute_sell(trade, current_price, "Manual Force Close", session)
        session.close()

    def forget_trade(self, symbol):
        # Remove from DB only (Fix for ghost trades)
        session = self.Session()
        try:
            trade = session.query(ActiveTrade).filter_by(symbol=symbol).first()
            if trade:
                session.delete(trade)
                session.commit()
                self.log(f"🗑️ Forgot Trade (DB Only): {symbol}")
        except Exception as e:
            session.rollback()
            self.log(f"Forget Error: {e}")
        finally:
            session.close()

    def sweep_dust(self):
        # Identify dust and convert to BNB
        try:
            session = self.Session()
            dust_trades = session.query(ActiveTrade).filter_by(is_dust=True).all()
            assets = [t.symbol.replace(self.quote_currency, '') for t in dust_trades]
            
            if assets:
                self.log(f"🧹 Sweeping: {assets}")
                self.client.transfer_dust(asset=assets)
                
                # Cleanup DB
                for t in dust_trades:
                    session.delete(t)
                session.commit()
        except Exception as e:
            self.log(f"Sweep Error: {e}")
        finally:
            session.close()

    def get_current_price_fast(self, symbol):
        # Try cache first
        with self.cache_lock:
            df = self.klines_cache.get(symbol)
            if df is not None:
                return df.iloc[-1]['close']
        # Fallback to API
        try:
            return float(self.client.ticker_price(symbol)['price'])
        except:
            return 0.0

    def get_wallet_balance(self, asset):
        try:
            acc = self.client.account()
            for b in acc['balances']:
                if b['asset'] == asset:
                    return float(b['free'])
            return 0.0
        except Exception as e:
            self.log(f"Balance Check Error: {e}")
            return 0.0
