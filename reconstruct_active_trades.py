
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from binance.spot import Spot
from database import DatabaseManager, ActiveTrade

# Load .env
load_dotenv()

def reconstruct_active_trades():
    print("--- Active Trades Reconstruction Tool ---")

    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')

    if not api_key or not api_secret:
        print("❌ Error: Missing API credentials in .env")
        return

    client = Spot(api_key=api_key, api_secret=api_secret)
    db = DatabaseManager()
    session = db.get_session()

    try:
        trades = session.query(ActiveTrade).all()
        if not trades:
            print("No active trades found in DB.")
            return

        for trade in trades:
            symbol = trade.symbol
            target_qty = trade.quantity
            print(f"\nScanning {symbol} (Target Qty: {target_qty})...")

            try:
                # Fetch recent trades (Buy & Sell)
                # We need enough history to cover the current position accumulation
                # Start from 2024-01-01 to be safe, or just last 500 trades
                # Using 500 limit is usually enough for a scalper
                history = client.my_trades(symbol=symbol, limit=500)
                history.sort(key=lambda x: x['time'], reverse=True) # Newest first

                accumulated_qty = 0.0
                accumulated_cost = 0.0
                orders_count = 0
                first_buy_time = None

                # We look for the sequence of BUYs that make up the current position.
                # If we hit a SELL that reduced position, we have to account for it?
                # FIFO logic is standard.
                # BUT, 'sync_wallet_positions' just looks at current balance.
                # So we just need the weighted average of the "remaining" coins.
                # This is equivalent to: iterating backwards, adding BUYs.
                # If we encounter a SELL, it consumes the most recent BUYs? No, typically FIFO consumes oldest.
                # However, for PnL calculation of a HOLDING, average price is usually calculated as:
                # Average Price = Total Cost Basis / Total Quantity.

                # Let's simplify:
                # We want to find the last N Buy orders that sum up to `target_qty`.
                # If there are Sells in between, it gets complicated (Trading logic).
                # Assumption: The bot does DCA (Buys) and then Sells everything.
                # So the current position is likely a chain of recent Buys WITHOUT Sells in between.
                # If there were Sells, they likely closed previous cycles.

                relevant_buys = []

                for h in history:
                    if h['isBuyer']:
                        qty = float(h['qty'])
                        price = float(h['price'])
                        quote_qty = float(h['quoteQty'])

                        relevant_buys.append({
                            'qty': qty,
                            'price': price,
                            'cost': quote_qty,
                            'time': h['time']
                        })

                        accumulated_qty += qty

                        if accumulated_qty >= target_qty * 0.99: # 1% tolerance
                            break
                    else:
                        # It's a SELL.
                        # If we see a SELL, it means the current cycle likely started AFTER this sell.
                        # (Unless it was a partial sell, but the bot usually sells 100%).
                        # So, if we hit a SELL, we should probably stop if we haven't reached target yet?
                        # Or maybe the position IS partial?
                        # Let's assume standard Bot behavior: Sell closes position.
                        # So any Buy before the last Sell belongs to a previous cycle.
                        # We stop here.
                        print(f"   Hit a SELL at {datetime.fromtimestamp(h['time']/1000)}. Assuming cycle start after this.")
                        break

                if not relevant_buys:
                    print("   ⚠️ No recent BUYs found to match this position.")
                    continue

                # Now calculate weighted average of the relevant buys
                # We might have collected more than needed in the last buy (partial fill logic)
                # But for simplicity, if the sum is close, we use them all.

                final_cost = 0.0
                final_qty = 0.0

                for b in relevant_buys:
                    final_cost += b['cost']
                    final_qty += b['qty']
                    first_buy_time = b['time'] # The last one in the loop is the oldest

                if final_qty == 0: continue

                avg_price = final_cost / final_qty

                # Update DB
                old_price = trade.entry_price
                trade.entry_price = avg_price
                trade.total_cost = final_cost # This might slightly mismatch if target_qty != final_qty, but it's consistent with avg_price

                # Safety Orders:
                # Base Order is #0. So Count = Total Orders - 1
                so_count = max(0, len(relevant_buys) - 1)
                trade.safety_order_count = so_count

                # Entry Time
                if first_buy_time:
                    trade.entry_time = datetime.fromtimestamp(first_buy_time/1000)

                # Next SO Price
                # Logic from bot: entry_price * (1 - step_scale)
                # We assume standard 2% step
                trade.next_safety_order_price = avg_price * 0.98

                print(f"   ✅ RECONSTRUCTED:")
                print(f"      Avg Price: {old_price:.4f} -> {avg_price:.4f}")
                print(f"      Total Cost: {final_cost:.2f}€")
                print(f"      Safety Orders: {so_count}")
                print(f"      Date: {trade.entry_time}")

            except Exception as e:
                print(f"   ❌ Error scanning {symbol}: {e}")

        session.commit()
        print("\n✅ Reconstruction Complete. Please restart the bot.")

    except Exception as e:
        session.rollback()
        print(f"❌ Database Error: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    reconstruct_active_trades()
