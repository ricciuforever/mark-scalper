from flask import Flask, render_template, jsonify, request
import os
from dotenv import load_dotenv
from trading_bot import TradingBot
import threading

load_dotenv()

app = Flask(__name__)

# Initialize Bot
BINANCE_API = os.getenv('BINANCE_API')
BINANCE_SECRET = os.getenv('BINANCE_SECRET')

bot = TradingBot(BINANCE_API, BINANCE_SECRET)

@app.route('/')
@app.route('/index.html')
def index():
    return render_template('index.html')

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
def start_bot():
    if not bot.is_running:
        bot.start()
        return jsonify({'status': 'started'})
    return jsonify({'status': 'already_running'})

@app.route('/api/stop', methods=['POST'])
def stop_bot():
    if bot.is_running:
        bot.stop()
        return jsonify({'status': 'stopped'})
    return jsonify({'status': 'not_running'})

@app.route('/api/config', methods=['POST'])
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

if __name__ == '__main__':
    debug_mode = os.getenv('APP_DEBUG', 'false').lower() == 'true'
    app.run(debug=debug_mode, host='0.0.0.0', port=5000, use_reloader=False)