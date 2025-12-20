import os
import threading
import logging
import hmac
import hashlib
from functools import wraps
from flask import Flask, render_template, jsonify, request, Response
from dotenv import load_dotenv
from mark_core import MarkBot
from database import DatabaseManager, ActiveTrade, TradeHistory
from sqlalchemy import func
from datetime import datetime, timedelta
import time
from binance.spot import Spot

# Load Env
load_dotenv()

app = Flask(__name__)
app.config['SECRET_KEY'] = 'markv2_secret_key'

# Basic Auth Credentials
ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD', 'admin')

# Initialize Bot
API_KEY = os.getenv('BINANCE_API_KEY', '')
API_SECRET = os.getenv('BINANCE_API_SECRET', '')

# Global Bot Instance
bot = MarkBot(API_KEY, API_SECRET)

# Helper for DB
db = DatabaseManager()

# --- Auth Decorator ---
def check_auth(username, password):
    """Checks credentials safely."""
    # Constant time comparison to prevent timing attacks
    user_ok = hmac.compare_digest(username, ADMIN_USER)
    pass_ok = hmac.compare_digest(password, ADMIN_PASSWORD)
    return user_ok and pass_ok

def authenticate():
    """Sends a 401 response that enables basic auth"""
    return Response(
    'Could not verify your access level for that URL.\n'
    'You have to login with proper credentials', 401,
    {'WWW-Authenticate': 'Basic realm="Mark V2 Login"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

# --- Routes ---

@app.route('/')
@requires_auth
def index():
    return render_template('index.html')

@app.route('/api/status')
@requires_auth
def status():
    session = db.get_session()
    try:
        active_trades = session.query(ActiveTrade).all()
        # Convert to list of dicts
        trades_data = []
        total_market_val = 0.0
        total_cost = 0.0

        for t in active_trades:
            # We can use the bot's fast price cache for display
            current_p = bot.get_current_price_fast(t.symbol)
            if current_p == 0: current_p = t.current_price

            val = t.quantity * current_p
            total_market_val += val
            total_cost += t.total_cost

            pnl = val - t.total_cost
            pnl_pct = (pnl / t.total_cost) * 100 if t.total_cost > 0 else 0

            trades_data.append({
                'symbol': t.symbol,
                'entry_price': t.entry_price,
                'quantity': t.quantity,
                'current_price': current_p,
                'pnl_abs': pnl,
                'pnl_pct': pnl_pct,
                'safety_orders': t.safety_order_count,
                'next_so_price': t.next_safety_order_price,
                'tp_price': t.entry_price * (1 + bot.tp_percent),
                'is_dust': t.is_dust
            })

        # Recent History
        history = session.query(TradeHistory).order_by(TradeHistory.close_time.desc()).limit(10).all()
        history_data = [{
            'symbol': h.symbol,
            'pnl_abs': h.pnl_abs,
            'pnl_pct': h.pnl_pct,
            'close_time': h.close_time.strftime('%H:%M:%S')
        } for h in history]

        # Get Balance (Real)
        real_wallet_balance = 0.0
        try:
             acc = bot.client.account()
             for b in acc['balances']:
                 if b['asset'] == 'EUR':
                     real_wallet_balance = float(b['free'])
                     break
        except Exception:
             real_wallet_balance = 1000.0 # Fallback

        total_invested = total_cost # Invested is cost basis, Market Value is separate

        # Calculate Capped Available Balance for Bot
        budget_remaining = max(0, bot.max_budget - total_invested)
        available_balance = min(real_wallet_balance, budget_remaining)

        # Total Balance displayed = Invested (Market Val) + Available (Capped)
        total_balance = available_balance + total_market_val

        # Calculate Total Realized PnL
        total_realized_pnl = session.query(func.sum(TradeHistory.pnl_abs)).scalar() or 0.0

        return jsonify({
            'running': bot.running,
            'status': bot.status_message,
            'active_trades': trades_data,
            'history': history_data,
            'total_balance': total_balance,
            'available_balance': available_balance,
            'invested_amount': total_market_val,
            'total_pnl': total_realized_pnl,
            'logs': bot.logs[:20]
        })
    finally:
        session.close()

@app.route('/api/settings', methods=['GET', 'POST'])
@requires_auth
def settings():
    if request.method == 'POST':
        data = request.json
        if 'base_order_size' in data:
            db.set_setting('base_order_size', data['base_order_size'])
        if 'max_safety_orders' in data:
            db.set_setting('max_safety_orders', data['max_safety_orders'])
        if 'max_budget' in data:
            db.set_setting('max_budget', data['max_budget'])

        # Reload bot settings
        bot.load_settings()
        return jsonify({'status': 'ok'})

    return jsonify({
        'base_order_size': bot.base_order_size,
        'max_safety_orders': bot.max_safety_orders,
        'dca_volume_scale': bot.dca_volume_scale,
        'dca_step_scale': bot.dca_step_scale,
        'max_budget': bot.max_budget
    })

@app.route('/api/control', methods=['POST'])
@requires_auth
def control():
    action = request.json.get('action')
    if action == 'start':
        bot.start()
    elif action == 'stop':
        bot.stop()
    elif action == 'sweep':
        # Run in thread to avoid blocking
        threading.Thread(target=bot.sweep_dust).start()
    elif action == 'close':
        symbol = request.json.get('symbol')
        if symbol:
            threading.Thread(target=bot.force_close, args=(symbol,)).start()
    elif action == 'forget':
        symbol = request.json.get('symbol')
        if symbol:
            threading.Thread(target=bot.forget_trade, args=(symbol,)).start()

    return jsonify({'status': 'ok'})

@app.route('/api/chart')
@requires_auth
def chart_data():
    session = db.get_session()
    try:
        # 1. Get History Dates
        trades = session.query(TradeHistory).order_by(TradeHistory.close_time).all()

        daily_pnl = {}
        first_date = None
        last_date = None

        for t in trades:
            d = t.close_time.strftime('%Y-%m-%d')
            if not first_date: first_date = d
            last_date = d
            daily_pnl[d] = daily_pnl.get(d, 0) + t.pnl_abs

        # If no history, return empty or dummy
        if not first_date:
            return jsonify({'dates': [], 'bot_pct': [], 'btc_pct': []})

        dates = sorted(list(daily_pnl.keys()))

        # 2. Build Bot Equity Curve
        bot_equity = []
        running_pnl = 0

        # Use max_budget as initial capital basis
        start_capital = bot.max_budget if bot.max_budget > 0 else 500.0

        for d in dates:
            running_pnl += daily_pnl[d]
            pct_gain = (running_pnl / start_capital) * 100
            bot_equity.append(pct_gain)

        # 3. Fetch Benchmarks for all Whitelisted Assets
        # We need daily klines from first_date to now
        benchmarks = {}
        try:
            # Convert start date to TS
            dt_obj = datetime.strptime(first_date, "%Y-%m-%d")
            start_ts = int(dt_obj.timestamp() * 1000)

            # Use a fresh client for this request to avoid conflict
            client = Spot() # Public data doesn't need keys

            # Iterate over all coins in whitelist
            for coin in bot.whitelist:
                symbol = f"{coin}EUR"
                try:
                    klines = client.klines(symbol, "1d", startTime=start_ts, limit=1000)

                    # Map Date -> Close Price
                    prices = {}
                    base_price = None

                    for k in klines:
                        # k[0] is Open Time
                        k_date = datetime.fromtimestamp(k[0]/1000).strftime('%Y-%m-%d')
                        close_p = float(k[4])
                        prices[k_date] = close_p

                        # Determine Base Price (Open price of first day)
                        if k_date == first_date and base_price is None:
                            base_price = float(k[1]) # Open

                    # Fallback
                    if base_price is None and klines:
                        base_price = float(klines[0][1])

                    # Generate Benchmark Data for each trade date
                    series = []
                    if base_price:
                        for d in dates:
                            # Find closest price
                            price = prices.get(d)
                            # If date missing (e.g. today not closed), use last known
                            if not price and prices:
                                price = list(prices.values())[-1]

                            if price:
                                pct = ((price - base_price) / base_price) * 100
                                series.append(pct)
                            else:
                                series.append(0.0)
                    else:
                        series = [0.0] * len(dates)

                    benchmarks[coin] = series

                except Exception as inner_e:
                    print(f"Benchmark Error for {coin}: {inner_e}")
                    benchmarks[coin] = [0.0] * len(dates)

        except Exception as e:
            print(f"Benchmark Error: {e}")

        return jsonify({
            'dates': dates,
            'bot_pct': bot_equity,
            'benchmarks': benchmarks
        })
    finally:
        session.close()

# Start Bot on App Startup
def start_bot_thread():
    # Wait a bit for DB init
    time.sleep(2)
    # Ensure keys are present before starting
    if API_KEY and API_SECRET:
        bot.start()
    else:
        print("⚠️ Bot not started automatically: Missing API Keys in .env")

if __name__ == '__main__':
    # In debug mode, Flask reloader spawns a child process.
    # WERKZEUG_RUN_MAIN is 'true' in that child process.
    # In production (no debug), it's not set, so we must start here.

    if os.environ.get("WERKZEUG_RUN_MAIN") == "true" or not app.debug:
        threading.Thread(target=start_bot_thread, daemon=True).start()

    app.run(host='0.0.0.0', port=5000)
