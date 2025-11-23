#!/bin/bash

# Configuration
VENV_DIR="venv"

echo "==================================================="
echo "     BINANCE SCALPER BOT - LINUX SERVER"
echo "==================================================="

# 1. Check/Create Virtual Environment
if [ ! -d "$VENV_DIR" ]; then
    echo "[1/3] Creating virtual environment..."
    python3 -m venv $VENV_DIR
fi

# 2. Install Dependencies
echo "[2/3] Checking dependencies..."
source $VENV_DIR/bin/activate
pip install -r requirements.txt

# 3. Start Application
echo "[3/3] Starting Bot Server..."
echo "Access the dashboard at http://<YOUR_SERVER_IP>:5000"
echo "Press Ctrl+C to stop."

python app.py
