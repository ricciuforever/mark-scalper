import os
import time
import json
import logging
import threading
from datetime import datetime, timedelta
import pandas as pd
import numpy as np
import math
from binance.spot import Spot
from binance.error import ClientError

# Autore: Emanuele Tolomei
# Data: 2025-11-23
# Descrizione: Trading Bot Scalper con gestione precisa LOT_SIZE e Saldo Reale

class TradingBot:
    def __init__(self, api_key, api_secret, initial_whitelist=None):
        self.api_key = api_key
        self.api_secret = api_secret

        self.trades_file = "trades.json"
        self.state_file = "active_trades.json"

        # --- Configurazione per il recupero storico ---
        self.HISTORY_START_TIME_MS = 1730419200000

        # Inizializzazione Client Binance
        if api_key and api_secret:
            self.client = Spot(api_key=api_key, api_secret=api_secret)
            try:
                self.client.account_status()
                print("✅ Binance API Connected successfully.")
                self.exchange_info = self.client.exchange_info() # Cache info exchange
            except Exception as e:
                self.client = None
                self.exchange_info = None
                print(f"❌ Binance API Connection Failed: {e}")
        else:
            self.client = None
            print("❌ CRITICAL: No API Keys provided. Bot cannot trade.")

        # Whitelist estesa
        self.whitelist = initial_whitelist if initial_whitelist else [
            'BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'ADA', 'DOGE', 'LINK', 'LTC', 'SUI',
            'AVAX', 'BCH', 'DOT', 'SHIB', 'PEPE', 'TRX', 'GRT', 'GMT', 'S', 'TRUMP',
            'ARB', 'APT', 'GALA', 'WIF', 'OP', 'PNUT', 'ATOM', 'EGLD', 'WIN', 'WLFI',
            'WLD', 'POL', 'VET', 'RENDER', 'XLM', 'ICP', 'NEAR'
        ]
        self.quote_currency = 'EUR'
        self.timeframe = '1m'
        self.trade_size = 10.0

        # --- Configurazione Dust ---
        self.DUST_THRESHOLD_EUR = 0.5

        # --- Strategy Configuration ---
        self.ATR_SL_MULTIPLIER = 2.0
        self.ATR_TP_MULTIPLIER = 3.0
        self.ATR_PERIOD = 14
        self.FIXED_TP_PERCENTAGE = 0.005  # Default TP 0.5%

        self.is_running = False
        self.logs = []
        self.lock = threading.Lock()
        self.current_status = "Idle"

        self.active_trades = self.load_active_trades()

        # Aggiorna TP per i trade esistenti caricati
        for sym, trade in self.active_trades.items():
            if trade.get('entry_price', 0) > 0:
                trade['tp_price'] = trade['entry_price'] * (1 + self.FIXED_TP_PERCENTAGE)

        self.recently_closed = []

        self.historical_trades_cache = self.load_historical_trades()
        self.benchmark_cache = {'data': [], 'timestamp': 0}

        if self.historical_trades_cache:
            self.recently_closed = sorted(self.historical_trades_cache, key=lambda x: x.get('close_time', ''), reverse=True)[:10]

    # --- PERSISTENZA ---

    def load_active_trades(self):
        if os.path.exists(self.state_file):
            try:
                with open(self.state_file, 'r') as f:
                    data = json.load(f)
                    for sym, trade in data.items():
                        if isinstance(trade['entry_time'], str):
                            try:
                                trade['entry_time'] = datetime.fromisoformat(trade['entry_time'])
                            except ValueError:
                                trade['entry_time'] = datetime.now()
                    return data
            except Exception as e:
                self.log(f"Errore caricamento active_trades: {e}")
        return {}

    def save_active_trades(self):
        try:
            data_to_save = {}
            for sym, trade in self.active_trades.items():
                trade_copy = trade.copy()
                if isinstance(trade_copy['entry_time'], datetime):
                    trade_copy['entry_time'] = trade_copy['entry_time'].isoformat()
                data_to_save[sym] = trade_copy
            with open(self.state_file, 'w') as f:
                json.dump(data_to_save, f, indent=4)
        except Exception as e:
            self.log(f"Errore salvataggio active_trades: {e}")

    def load_historical_trades(self):
        if os.path.exists(self.trades_file):
            try:
                with open(self.trades_file, 'r') as f:
                    data = json.load(f)
                    if data:
                        return data
            except:
                pass
        return []

    def save_historical_trades(self):
        with open(self.trades_file, 'w') as f:
            json.dump(self.historical_trades_cache, f, indent=4)

    # --- UTILITÀ PER LOT SIZE E FILTRI (CRITICO) ---
    def get_symbol_step_size(self, symbol):
        """Recupera lo stepSize corretto per il simbolo dalla cache exchange_info."""
        if not self.exchange_info:
            try:
                self.exchange_info = self.client.exchange_info()
            except:
                return 8 # Fallback default

        for s in self.exchange_info['symbols']:
            if s['symbol'] == symbol:
                for f in s['filters']:
                    if f['filterType'] == 'LOT_SIZE':
                        return float(f['stepSize'])
        return 0.00000001 # Fallback safe

    def round_to_step_size(self, quantity, step_size):
        """Arrotonda la quantità in base allo stepSize richiesto da Binance."""
        if step_size == 0: return quantity
        precision = int(round(-math.log(step_size, 10), 0))
        # Tronca al ribasso alla precisione corretta
        factor = 10 ** precision
        return math.floor(quantity * factor) / factor

    # --- LOGICHE DI SINCRONIZZAZIONE ---
    def sync_orphan_positions(self):
        """Allinea active_trades con il saldo reale del wallet."""
        if not self.client: return
        try:
            self.log("🔍 Sincronizzazione posizioni wallet...")
            account = self.client.account()
            balances = {b['asset']: float(b['free']) for b in account['balances'] if float(b['free']) > 0}

            for coin in self.whitelist:
                symbol = f"{coin}{self.quote_currency}"

                # Se abbiamo saldo ma il trade non è in memoria, lo recuperiamo
                if coin in balances:
                    qty = balances[coin]
                    current_price = self.get_current_price(symbol)
                    value_eur = qty * current_price

                    if value_eur < 0.1: continue # Ignoriamo polvere < 0.10 EUR

                    if symbol not in self.active_trades:
                        self.log(f"♻️ Trovata posizione orfana: {symbol} ({qty} {coin} - {value_eur:.2f}€). Recupero...")

                        # Recuperiamo ATR per SL/TP
                        df = self.fetch_ohlcv(symbol, limit=self.ATR_PERIOD * 2)
                        atr = 0.0
                        if df is not None and not pd.isna(df.iloc[-1]['atr']):
                             atr = df.iloc[-1]['atr']

                        atr_sl = atr * self.ATR_SL_MULTIPLIER if atr > 0 else current_price * 0.02
                        # atr_tp = atr * self.ATR_TP_MULTIPLIER if atr > 0 else current_price * 0.03 # Unused, using fixed TP

                        is_dust = value_eur < self.DUST_THRESHOLD_EUR

                        self.active_trades[symbol] = {
                            'entry_price': current_price, # Approssimazione
                            'entry_time': datetime.now(),
                            'quantity': qty,
                            'sl_price': current_price - atr_sl,
                            'tp_price': current_price * (1 + self.FIXED_TP_PERCENTAGE),
                            'recovered': True,
                            'is_dust': is_dust
                        }
                    else:
                        # Aggiorniamo la quantità reale se differisce (es. parziali fill)
                        self.active_trades[symbol]['quantity'] = qty
                        # Forziamo l'aggiornamento del TP anche qui per sicurezza
                        if self.active_trades[symbol].get('entry_price', 0) > 0:
                            self.active_trades[symbol]['tp_price'] = self.active_trades[symbol]['entry_price'] * (1 + self.FIXED_TP_PERCENTAGE)

            # Pulizia inversa: Se abbiamo un trade in memoria ma saldo 0, lo rimuoviamo
            to_remove = []
            for sym in self.active_trades:
                base_asset = sym.replace(self.quote_currency, '')
                if base_asset not in balances or balances[base_asset] < (0.1 / self.active_trades[sym].get('entry_price', 1)):
                    # Doppio check: è veramente vuoto?
                    real_qty = self.get_coin_free_balance(base_asset)
                    val = real_qty * self.get_current_price(sym)
                    if val < 0.1:
                        to_remove.append(sym)

            for sym in to_remove:
                self.log(f"🧹 Pulizia trade fantasma (saldo 0): {sym}")
                del self.active_trades[sym]

            self.save_active_trades()
        except Exception as e:
            self.log(f"Sync Error: {e}")

    # --- CORE ---
    def get_whitelist_pairs(self):
        return [f"{coin}{self.quote_currency}" for coin in self.whitelist]

    def log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        entry = f"[{timestamp}] {message}"
        with self.lock:
            self.logs.insert(0, entry)
            if len(self.logs) > 200: self.logs.pop()
        print(entry)

    def fetch_ohlcv(self, symbol, limit=50):
        if not self.client: return None
        try:
            klines = self.client.klines(symbol, self.timeframe, limit=limit)
            df = pd.DataFrame(klines, columns=['ts', 'open', 'high', 'low', 'close', 'vol', 'ct', 'qav', 'n', 'tbv', 'tqv', 'i'])
            df[['open', 'high', 'low', 'close', 'vol']] = df[['open', 'high', 'low', 'close', 'vol']].astype(float)
            df = self.calculate_indicators(df)
            return df
        except Exception as e:
            if "429" in str(e) or "503" in str(e):
                self.log("⚠️ API Limit. Pause 10s...")
                time.sleep(10)
            return None

    def calculate_indicators(self, df):
        delta = df['close'].diff()
        gain = (delta.where(delta > 0, 0)).rolling(14).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(14).mean().replace(0, 0.000001)
        rs = gain / loss
        df['rsi'] = 100 - (100 / (1 + rs))
        df['sma'] = df['close'].rolling(20).mean()
        df['std'] = df['close'].rolling(20).std()
        df['lower_band'] = df['sma'] - (2 * df['std'])

        high_low = df['high'] - df['low']
        high_close = np.abs(df['high'] - df['close'].shift())
        low_close = np.abs(df['low'] - df['close'].shift())
        ranges = pd.concat([high_low, high_close, low_close], axis=1)
        true_range = ranges.max(axis=1)
        df['atr'] = true_range.ewm(span=self.ATR_PERIOD, min_periods=self.ATR_PERIOD).mean()

        return df

    def get_current_price(self, symbol):
        if not self.client: return 0.0
        try:
            return float(self.client.ticker_price(symbol)['price'])
        except:
            return 0.0

    def start(self):
        if self.is_running: return
        if not self.client:
            self.log("❌ Cannot start: No API Keys.")
            return

        self.sync_orphan_positions() # Sincronizza all'avvio
        self.is_running = True
        self.log("Bot started (REAL TRADING).")
        self.thread = threading.Thread(target=self.run_loop)
        self.thread.daemon = True
        self.thread.start()

    def stop(self):
        self.is_running = False
        self.log("Bot stopping...")

    def update_trade_data(self, symbol):
        trade = self.active_trades[symbol]
        curr = self.get_current_price(symbol)
        if curr == 0.0: return

        trade['current_price'] = curr
        trade['pnl_pct'] = (curr - trade['entry_price']) / trade['entry_price']
        trade['pnl_abs'] = (curr - trade['entry_price']) * trade['quantity']

    def run_loop(self):
        while self.is_running:
            try:
                self.process_cycle()
            except Exception as e:
                self.log(f"Loop Error: {e}")

            for _ in range(5):
                if not self.is_running: break
                time.sleep(1)

    def process_cycle(self):
        pairs = self.get_whitelist_pairs()

        # Aggiorniamo cache info exchange occasionalmente (ogni 100 cicli circa) se necessario
        # qui lo diamo per buono all'init per performance

        for symbol in pairs:
            if not self.is_running: break
            time.sleep(1.0) # Leggera pausa

            active_trade = self.active_trades.get(symbol)

            if active_trade:
                self.update_trade_data(symbol)

                # Se è stato flaggato come errore in precedenza, riproviamo a gestirlo
                status_suffix = ""
                if active_trade.get('error_state'):
                     status_suffix = " (RETRYING ERROR)"

                if active_trade.get('is_dust', False):
                    self.current_status = f"Watching Dust {symbol}"
                    # Dust non fa nulla, solo monitoraggio
                else:
                    self.current_status = f"Managing {symbol}{status_suffix}"
                    self.manage_trade(symbol)
            else:
                self.current_status = f"Scanning {symbol}"
                self.scan_for_entry(symbol)

        self.save_active_trades()

    def scan_for_entry(self, symbol):
        # Controllo sicurezza: non aprire se ho già MAX_TRADES aperti (opzionale, metto 10)
        if len([t for t in self.active_trades.values() if not t.get('is_dust')]) >= 10:
            return

        df = self.fetch_ohlcv(symbol)
        if df is None or len(df) < self.ATR_PERIOD: return

        last = df.iloc[-1]
        if pd.isna(last['atr']): return

        # Condizione di acquisto
        if last['rsi'] < 30 and last['close'] < last['lower_band']:
            self.execute_buy(symbol, last['close'], last['atr'])

    def execute_buy(self, symbol, price, atr_value):
        if not self.client: return

        try:
            self.log(f"🛒 Buying {symbol} ({self.trade_size}€)...")
            order = self.client.new_order(symbol=symbol, side='BUY', type='MARKET', quoteOrderQty=self.trade_size)

            executed_qty = float(order['executedQty'])
            quote_spent = float(order['cummulativeQuoteQty'])
            avg_price = quote_spent / executed_qty if executed_qty > 0 else price

            atr_sl_distance = atr_value * self.ATR_SL_MULTIPLIER
            # atr_tp_distance = atr_value * self.ATR_TP_MULTIPLIER # Unused

            position_value_eur = avg_price * executed_qty
            is_dust = position_value_eur < self.DUST_THRESHOLD_EUR

            self.active_trades[symbol] = {
                'entry_price': avg_price,
                'entry_time': datetime.now(),
                'quantity': executed_qty,
                'sl_price': avg_price - atr_sl_distance,
                'tp_price': avg_price * (1 + self.FIXED_TP_PERCENTAGE),
                'is_dust': is_dust,
                'error_state': False
            }
            if is_dust:
                self.log(f"🧹 OPENED DUST {symbol} | Value: {position_value_eur:.4f}€")
            else:
                self.log(f"✅ OPENED LONG {symbol} | Price: {avg_price:.4f}")

        except Exception as e:
            self.log(f"❌ Buy Failed {symbol}: {e}")

    def manage_trade(self, symbol):
        trade = self.active_trades[symbol]
        curr = trade.get('current_price', 0.0)

        if curr >= trade['tp_price']:
            self.execute_sell(symbol, curr, "ATR Fixed TP")
            return

        elif curr <= trade['sl_price']:
            self.execute_sell(symbol, curr, "ATR Fixed SL")
            return

    def archive_and_delete(self, symbol, price, reason, pnl_abs, pnl_pct):
        # Archivia nello storico
        closed_trade = {
            'symbol': symbol,
            'entry_price': self.active_trades[symbol]['entry_price'],
            'exit_price': price,
            'quantity': self.active_trades[symbol]['quantity'],
            'pnl_abs': pnl_abs,
            'pnl_pct': pnl_pct,
            'reason': reason,
            'close_time': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }

        self.recently_closed.insert(0, closed_trade)
        if len(self.recently_closed) > 20: self.recently_closed.pop()
        self.historical_trades_cache.append(closed_trade)
        self.save_historical_trades()

        # RIMUOVIAMO DAGLI ATTIVI
        del self.active_trades[symbol]

    def execute_sell(self, symbol, price, reason):
        if not self.client: return
        trade = self.active_trades[symbol]

        try:
            self.log(f"📉 Selling {symbol} ({reason})...")

            # 1. Recupera Balance REALE da Binance
            base_coin = symbol.replace(self.quote_currency, '')
            actual_balance = self.get_coin_free_balance(base_coin)

            # 2. Recupera Step Size Corretto (CRITICO PER LOT_SIZE ERROR)
            step_size = self.get_symbol_step_size(symbol)
            qty_to_sell = self.round_to_step_size(actual_balance, step_size)

            if qty_to_sell <= 0:
                self.log(f"⚠️ Sell Skipped {symbol}: Qty {actual_balance} too low for step {step_size}. Marking as DUST.")
                trade['is_dust'] = True # Non proviamo più a venderlo attivamente
                trade['error_state'] = False
                return

            # 3. Esegui Vendita
            order = self.client.new_order(symbol=symbol, side='SELL', type='MARKET', quantity=qty_to_sell)

            # 4. Calcolo PnL Finale Reale
            if 'cummulativeQuoteQty' in order:
                total_eur_received = float(order['cummulativeQuoteQty'])
                real_exit_price = total_eur_received / float(order['executedQty'])
            else:
                real_exit_price = price # Fallback
                total_eur_received = real_exit_price * qty_to_sell

            cost_basis = trade['entry_price'] * qty_to_sell
            pnl_abs = total_eur_received - cost_basis
            pnl_pct = (real_exit_price - trade['entry_price']) / trade['entry_price']

            self.log(f"💰 CLOSED {symbol} | PnL: {pnl_abs:.4f}€")

            # 5. Archivia e Rimuovi (SOLO SU SUCCESSO)
            self.archive_and_delete(symbol, real_exit_price, reason, pnl_abs, pnl_pct)

        except ClientError as e:
            self.log(f"❌ Sell Failed {symbol}: {e.error_message}")
            # NON CANCELLIAMO IL TRADE. LO SEGNIAMO IN ERRORE.
            trade['error_state'] = True

        except Exception as e:
            self.log(f"❌ Sell Error {symbol}: {e}")
            trade['error_state'] = True

    # --- UTILITY FINANZIARIE ---
    def get_coin_free_balance(self, coin):
        if not self.client: return 0.0
        try:
            acc = self.client.account()
            for b in acc['balances']:
                if b['asset'] == coin:
                    return float(b['free'])
        except: pass
        return 0.0

    def get_eur_balance(self):
        return self.get_coin_free_balance('EUR')

    def get_status_data(self):
        active_trades_data = {}
        total_market_value_in_trades = 0.0

        # 1. Saldo Cash
        wallet_balance_free_eur = self.get_eur_balance()

        # 2. Valutazione Posizioni Attive
        for sym, trade in self.active_trades.items():
             trade_copy = trade.copy()
             current_price = trade.get('current_price', 0.0)

             # Calcolo valore di mercato live
             market_val = current_price * trade.get('quantity', 0.0)
             total_market_value_in_trades += market_val

             # Flag visuale per UI se in errore
             if trade.get('error_state'):
                 trade_copy['reason_display'] = "⚠️ SELL ERROR - RETRYING"

             active_trades_data[sym] = trade_copy

        # 3. Equity Totale Reale
        total_equity = wallet_balance_free_eur + total_market_value_in_trades

        return {
            'is_running': self.is_running,
            'current_status': self.current_status,
            'logs': self.logs[:50],
            'active_trades': active_trades_data,
            'closed_trades': self.recently_closed,
            'total_balance': total_equity,         # Questo ora riflette TUTTO
            'available_balance': wallet_balance_free_eur
        }

    def get_historical_pnl(self):
        df = pd.DataFrame(self.historical_trades_cache)
        if df.empty:
            return {'dates': [], 'bot_pnl': [], 'btc_pnl': [], 'total_realized_pnl': 0.0}

        df['close_time'] = pd.to_datetime(df['close_time'])
        df['date_str'] = df['close_time'].dt.strftime('%Y-%m-%d')
        daily_pnl = df.groupby('date_str')['pnl_abs'].sum().cumsum()

        dates = daily_pnl.index.tolist()
        bot_pnl = daily_pnl.values.tolist()
        total_realized = df['pnl_abs'].sum()

        # Calcolo Capitale Iniziale Stimato
        try:
            wallet_balance = self.get_eur_balance()
            active_value = sum(t.get('current_price', 0) * t.get('quantity', 0) for t in self.active_trades.values())
            current_equity = wallet_balance + active_value
            initial_capital = current_equity - total_realized
            if initial_capital <= 0: initial_capital = 1000.0 # Fallback
        except:
            initial_capital = 1000.0

        # Calcolo Benchmark BTC Buy & Hold (Cached 1h)
        btc_pnl_pct = []
        cache_valid = (time.time() - self.benchmark_cache['timestamp']) < 3600

        if dates and self.client:
            if cache_valid and self.benchmark_cache['data'] and len(self.benchmark_cache['data']) == len(dates):
                btc_pnl_pct = self.benchmark_cache['data']
            else:
                try:
                    start_date = datetime.strptime(dates[0], "%Y-%m-%d")
                    fetch_start = start_date - timedelta(days=1)
                    start_ts = int(fetch_start.timestamp() * 1000)

                    # Fetch daily klines BTCEUR
                    klines = self.client.klines("BTCEUR", "1d", startTime=start_ts, limit=1000)

                    btc_prices = {}
                    base_price = None

                    for k in klines:
                        d_obj = datetime.fromtimestamp(k[0]/1000)
                        d_str = d_obj.strftime('%Y-%m-%d')
                        close_price = float(k[4])
                        btc_prices[d_str] = close_price

                        # Cerchiamo il prezzo di apertura del primo giorno utile
                        if d_str == dates[0]:
                            base_price = float(k[1]) # Open price

                    # Fallback se non troviamo il giorno esatto
                    if base_price is None and klines:
                        base_price = float(klines[0][1])

                    if base_price:
                        for d in dates:
                            price = btc_prices.get(d)
                            if price:
                                # PnL % = (Current - Base) / Base * 100
                                pct = ((price - base_price) / base_price) * 100
                                btc_pnl_pct.append(pct)
                            else:
                                btc_pnl_pct.append(btc_pnl_pct[-1] if btc_pnl_pct else 0.0)

                        # Update cache
                        self.benchmark_cache['data'] = btc_pnl_pct
                        self.benchmark_cache['timestamp'] = time.time()
                    else:
                         btc_pnl_pct = [0.0] * len(dates)

                except Exception as e:
                    self.log(f"⚠️ BTC Benchmark Error: {e}")
                    btc_pnl_pct = [0.0] * len(dates)
        else:
             btc_pnl_pct = [0.0] * len(dates)

        # Calcolo Bot PnL %
        bot_pnl_pct = []
        if initial_capital > 0:
            bot_pnl_pct = [(x / initial_capital) * 100 for x in bot_pnl]
        else:
            bot_pnl_pct = [0.0] * len(bot_pnl)

        return {
            'dates': dates,
            'bot_pnl': bot_pnl,           # Keep for legacy/debug if needed
            'bot_pnl_pct': bot_pnl_pct,   # NEW
            'btc_pnl_pct': btc_pnl_pct,   # NEW
            'total_realized_pnl': total_realized
        }