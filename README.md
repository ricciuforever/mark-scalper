# Binance Scalper Bot (Server Edition)

A simple, local trading bot designed for "hit and run" scalping strategies on Binance Spot (EUR pairs).
Optimized for deployment on a Linux Server (e.g., VPS or Dedicated).

## Features
- **Strategy**: Buy on Bear Market Bounces (RSI < 30 & Price < Lower Bollinger Band).
- **Pairs**: Whitelist of EUR pairs (BTC, ETH, SOL, etc.).
- **Paper Trading**: Simulation mode included (enabled by default).
- **Interface**: Web-based dashboard (Dark Mode) to control the bot and view logs.
- **Safety**: "Hit and Run" logic (0.5% profit target or 5 min max duration).

## Installation & Usage (Linux Server)

### 1. Setup
Navigate to your directory (e.g., `/var/www/vhosts/capitaltrading.it/mark.capitaltrading.it`):
```bash
cd /var/www/vhosts/capitaltrading.it/mark.capitaltrading.it
chmod +x start.sh
```

### 2. Manual Run (Testing)
To run the bot manually in the terminal:
```bash
./start.sh
```
Access the dashboard at `http://<YOUR_SERVER_IP>:5000`.

### 3. Continuous Run (Systemd Service)
To keep the bot running in the background and restart on failure, use the provided `mark_trader.service` file.

1. **Edit the Service File**:
   Open `mark_trader.service` and ensure the paths (`WorkingDirectory` and `ExecStart`) match your actual installation path.

2. **Install Service**:
   ```bash
   sudo cp mark_trader.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl enable mark_trader
   sudo systemctl start mark_trader
   ```

3. **Check Status**:
   ```bash
   sudo systemctl status mark_trader
   ```

4. **View Logs**:
   ```bash
   journalctl -u mark_trader -f
   ```

## Configuration
- **API Keys**: stored in `.env`.
- **Debug Mode**: set `APP_DEBUG=false` in `.env` for production.
- **Whitelist**: Editable directly from the Dashboard.

## Requirements
- Python 3.8+ installed.
- Pip installed.
