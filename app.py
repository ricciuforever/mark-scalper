from flask import Flask, render_template, jsonify, request, Response
import os
import hmac
from functools import wraps
from dotenv import load_dotenv
from trading_bot import TradingBot
import threading

load_dotenv()

app = Flask(__name__)

# Initialize Bot
BINANCE_API = os.getenv('BINANCE_API')
BINANCE_SECRET = os.getenv('BINANCE_SECRET')

bot = TradingBot(BINANCE_API, BINANCE_SECRET)

# Authentication Configuration
ADMIN_USER = os.getenv('ADMIN_USER', 'admin')
ADMIN_PASSWORD = os.getenv('ADMIN_PASSWORD')

if not ADMIN_PASSWORD:
    print("⚠️ WARNING: ADMIN_PASSWORD not set in environment variables. Admin access will be disabled or insecure.")

def check_auth(username, password):
    """Checks if the username and password combination is valid."""
    if not ADMIN_PASSWORD:
        return False

    # Use hmac.compare_digest for constant-time comparison to prevent timing attacks
    user_match = hmac.compare_digest(username, ADMIN_USER)
    pass_match = hmac.compare_digest(password, ADMIN_PASSWORD)

    return user_match and pass_match

def authenticate():
    """Sends a 401 response that enables basic auth."""
    return Response(
        'Could not verify your access level for that URL.\n'
        'You have to login with proper credentials', 401,
        {'WWW-Authenticate': 'Basic realm="Login Required"'})

def requires_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        if not auth or not check_auth(auth.username, auth.password):
            return authenticate()
        return f(*args, **kwargs)
    return decorated

@app.route('/')
@app.route('/index.html')
def index():
    return render_template('index.html')

@app.route('/admin')
@requires_auth
def admin():
    return render_template('admin.html')

@app.route('/api/status')
def status():
    return jsonify(bot.get_status_data())

@app.route('/api/stats')
def stats():
    history = bot.get_historical_pnl()
    return jsonify({
        'total_pnl': history['total_realized_pnl'],
        'chart_data': history
    })

@app.route('/api/start', methods=['POST'])
@requires_auth
def start_bot():
    if not bot.is_running:
        bot.start()
        return jsonify({'status': 'started'})
    return jsonify({'status': 'already_running'})

@app.route('/api/stop', methods=['POST'])
@requires_auth
def stop_bot():
    if bot.is_running:
        bot.stop()
        return jsonify({'status': 'stopped'})
    return jsonify({'status': 'not_running'})

@app.route('/api/config', methods=['POST'])
@requires_auth
def update_config():
    data = request.json

    if 'whitelist' in data:
        raw_list = data['whitelist'].split(',')
        cleaned_list = [s.strip().upper() for s in raw_list if s.strip()]
        bot.whitelist = cleaned_list

    if 'trade_size' in data:
        try:
            bot.trade_size = float(data['trade_size'])
        except ValueError:
            pass

    bot.log(f"Config Updated: Size={bot.trade_size}€, Whitelist Count={len(bot.whitelist)}")
    return jsonify({'status': 'success'})

@app.route('/api/close/<symbol>', methods=['POST'])
@requires_auth
def close_trade(symbol):
    success, msg = bot.force_close_trade(symbol)
    if success:
        return jsonify({'status': 'success', 'message': msg})
    else:
        return jsonify({'status': 'error', 'message': msg}), 400

@app.route('/api/sweep_dust', methods=['POST'])
@requires_auth
def sweep_dust():
    success, msg = bot.convert_dust_to_bnb()
    if success:
        return jsonify({'status': 'success', 'message': msg})
    else:
        return jsonify({'status': 'error', 'message': msg}), 400

if __name__ == '__main__':
    debug_mode = os.getenv('APP_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000, use_reloader=False)