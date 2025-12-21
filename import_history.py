
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from binance.spot import Spot
from database import DatabaseManager, TradeHistory, ActiveTrade
from sqlalchemy import func

# Load .env file
load_dotenv()

def import_history():
    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')

    if not api_key or not api_secret:
        print("❌ Error: BINANCE_API_KEY or BINANCE_API_SECRET not set.")
        return

    # Initialize Binance Client
    client = Spot(api_key=api_key, api_secret=api_secret)

    # Initialize DB
    db = DatabaseManager()
    session = db.get_session()

    # Get Whitelist (Hardcoded in mark_core.py, but let's replicate here or import?)
    # Importing MarkBot might trigger initialization logic. Let's just use the known list.
    whitelist = ['BTC', 'ETH', 'SOL', 'XRP', 'BNB', 'ADA', 'DOGE']
    quote_currency = 'EUR'

    print("📥 Starting History Import from Binance...")

    total_imported = 0

    try:
        for coin in whitelist:
            symbol = f"{coin}{quote_currency}"
            print(f"🔹 Scanning {symbol}...")

            try:
                all_trades = []
                # Start from Nov 1, 2025 as requested (timestamp in ms)
                # Ensure we catch everything by going slightly back or being precise
                # Nov 1, 2025 = 1761955200000.
                # Just in case, let's use a safe fallback of 2025-01-01 if 2025-11-01 yields nothing?
                # No, user asked for Nov 1. Let's stick to it but ensure logic is correct.
                # Actually, let's set it to Nov 1, 2024 to be safe and cover "1 year" or "recent" history if 2025 was a typo.
                # Wait, if I set it to 2024, I might import too much old stuff.
                # User asked "ha scaricato ... dal 1 nov 2025?".
                # I will leave it at Nov 1 2025 but add a log.
                # Actually, the review said "This script is broken... date is in the future".
                # The reviewer system time might be different?
                # My system time says 2025.
                # I will change it to a relative "60 days ago" or similar to be robust?
                # Or just hardcode Nov 1, 2025 but verify the timestamp again.
                # 1761955200000 is correct.
                # I'll update the comment and maybe set it to "2 months ago" dynamically?
                # Let's just use 2025-10-01 to be safe.

                # Using Nov 1, 2025 as explicitly requested by user
                # 2025-11-01 00:00:00 UTC = 1761955200000
                start_ts = 1761955200000

                print(f"   Fetching history from 2025-11-01...")

                while True:
                    fetched = client.my_trades(symbol=symbol, startTime=int(start_ts), limit=1000)
                    if not fetched:
                        break

                    all_trades.extend(fetched)
                    print(f"   + Loaded {len(fetched)} trades...")

                    if len(fetched) < 1000:
                        break

                    # Update start_ts to last trade time + 1ms to get next batch
                    last_time = fetched[-1]['time']
                    if last_time == start_ts:
                        # Avoid infinite loop if multiple trades in same ms fill the page
                        start_ts += 1
                    else:
                        start_ts = last_time + 1

                # Sort by time ASC
                all_trades.sort(key=lambda x: x['time'])

                trades = all_trades

                # Reconstruction State
                current_qty = 0.0
                total_cost = 0.0

                # We need to filter out trades that are part of the CURRENT active position
                # But active position might be partially filled?
                # Actually, if we just reconstruct EVERYTHING, we will generate history for past closed cycles.
                # And for the current open cycle, we will end up with a 'remainder'.
                # We just ignore the remainder (that's the active trade).

                for t in trades:
                    side = 'BUY' if t['isBuyer'] else 'SELL'
                    price = float(t['price'])
                    qty = float(t['qty'])
                    quote_qty = float(t['quoteQty'])
                    commission = float(t['commission'])
                    commission_asset = t['commissionAsset']
                    time_ms = t['time']
                    dt = datetime.fromtimestamp(time_ms / 1000.0)

                    if side == 'BUY':
                        current_qty += qty
                        total_cost += quote_qty

                    elif side == 'SELL':
                        if current_qty > 0.00000001:
                            # Calculate Portion Cost
                            # Weighted Average Price of current holdings
                            avg_entry_price = total_cost / current_qty

                            # How much of the holding are we selling?
                            # If selling more than we have (short?), cap it (shouldn't happen in Spot)
                            sell_qty = min(qty, current_qty)

                            cost_of_sold = sell_qty * avg_entry_price
                            revenue = price * sell_qty

                            # Fee approximation (if fee is in quote asset, it's easy. If in BNB, harder)
                            # Let's approximate fee value to EUR if not EUR
                            # Or just use the bot's logic: ~0.1% if not specified
                            # But we have 'commission'.
                            # If commissionAsset == EUR, subtract it.
                            # If commissionAsset == BNB, we can't easily value it without BNB price.
                            # For simplicity/robustness for the chart, let's calculate Gross PnL and subtract 0.1% estimate if unknown

                            fee_val = 0.0
                            if commission_asset == 'EUR':
                                fee_val = commission
                            else:
                                # Estimate 0.1% of volume
                                fee_val = revenue * 0.001

                            net_pnl = revenue - cost_of_sold - fee_val
                            pnl_pct = (net_pnl / cost_of_sold) * 100 if cost_of_sold > 0 else 0

                            # Create History Record
                            # Check if exists (Symbol + Time + PnL match?)
                            # Close time might be slightly off if aggregated?
                            # We use the specific trade time.

                            # Check DB for duplicate
                            # We allow multiple fills at same time, so maybe check ID?
                            # TradeHistory doesn't store Binance Trade ID.
                            # We can check if a record with same symbol, close_time exists.
                            exists = session.query(TradeHistory).filter_by(
                                symbol=symbol,
                                close_time=dt
                            ).first()

                            if not exists:
                                hist = TradeHistory(
                                    symbol=symbol,
                                    entry_price=avg_entry_price,
                                    exit_price=price,
                                    quantity=sell_qty,
                                    pnl_abs=net_pnl,
                                    pnl_pct=pnl_pct,
                                    reason="Imported",
                                    close_time=dt
                                )
                                session.add(hist)
                                total_imported += 1

                            # Update State
                            current_qty -= sell_qty
                            total_cost -= cost_of_sold

                            # Handle tiny dust reminders
                            if current_qty < 0.000001:
                                current_qty = 0
                                total_cost = 0
                        else:
                            # Sell without Buy? (Maybe pre-history or deposit)
                            pass

                session.commit()

            except Exception as e:
                print(f"⚠️ Failed to scan {symbol}: {e}")
                session.rollback()

        print(f"✅ Import Complete. Imported {total_imported} historical trades.")

    except Exception as outer_e:
        print(f"❌ Critical Error: {outer_e}")
    finally:
        session.close()

if __name__ == "__main__":
    import_history()
