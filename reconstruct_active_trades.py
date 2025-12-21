
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from binance.spot import Spot
from database import DatabaseManager, ActiveTrade

# Load .env
load_dotenv()

def reconstruct_active_trades():
    print("--- Active Trades Reconstruction Tool (Backward Scan) ---")

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
            print(f"\nScanning {symbol} (Target Qty: {target_qty:.6f})...")

            try:
                # Fetch recent trades (Buy & Sell)
                # We fetch enough history (e.g. 500 trades) to cover the current position
                # We do NOT use a date filter here, we just want the most recent activity
                history = client.my_trades(symbol=symbol, limit=500)
                # Sort newest first
                history.sort(key=lambda x: x['time'], reverse=True)

                accumulated_qty = 0.0
                relevant_buys = []
                stop_scan = False

                for h in history:
                    if stop_scan: break

                    if h['isBuyer']:
                        qty = float(h['qty'])
                        price = float(h['price'])
                        cost = float(h['quoteQty'])

                        # How much of this buy do we need?
                        remaining_needed = target_qty - accumulated_qty

                        if remaining_needed <= 0.00000001:
                            break

                        # If this buy is larger than what we need (e.g. we sold some of it), take partial
                        take_qty = min(qty, remaining_needed)
                        take_cost = (take_qty / qty) * cost

                        relevant_buys.append({
                            'qty': take_qty,
                            'price': price,
                            'cost': take_cost,
                            'time': h['time'],
                            'orderId': h['orderId']
                        })

                        accumulated_qty += take_qty

                    else:
                        # IT IS A SELL
                        # Logic: If we hit a Sell, it means the coins we hold NOW were bought AFTER this sell.
                        # (Unless we are holding a "long term bag" from before the sell, but the Bot strategy is usually clear-out).
                        # Assuming FIFO/Bot logic: Valid inventory must be YOUNGER than the last full clear-out.
                        # However, to be safe against partial sells:
                        # We are reconstructing "What buys make up the CURRENT wallet balance?"
                        # The standard accounting assumption is LIFO for cost basis? No, FIFO.
                        # If FIFO: The coins we hold are the LAST ones bought.
                        # So simply summing the most recent Buys until we match Quantity is the correct "Recent Cost Basis".
                        # The presence of Sells is irrelevant *unless* those Sells consumed the recent Buys.
                        # But Sells consume OLD buys (FIFO).
                        # So, the MOST RECENT Buys are definitely still in our wallet (unless we sold MORE than we held, impossible).
                        # THEREFORE: We can ignore Sells and just sum the recent Buys.
                        pass

                # Check if we found enough
                if accumulated_qty < (target_qty * 0.9):
                    print(f"   ⚠️ Warning: Could only trace {accumulated_qty:.6f} coins (Target: {target_qty:.6f}).")
                    print("      History limit (500) might be too short or coins are very old.")
                    # We will use what we found, but warn

                if not relevant_buys:
                    print("   ⚠️ No relevant buys found.")
                    continue

                # Calculate Stats
                final_cost = sum(b['cost'] for b in relevant_buys)
                final_qty = sum(b['qty'] for b in relevant_buys) # Should match accumulated_qty

                if final_qty == 0: continue

                weighted_avg_price = final_cost / final_qty

                # Safety Orders
                unique_order_ids = set(b['orderId'] for b in relevant_buys)
                total_orders = len(unique_order_ids)
                so_count = max(0, total_orders - 1)

                # Entry Time (Oldest of the relevant buys)
                entry_ts = relevant_buys[-1]['time']
                entry_dt = datetime.fromtimestamp(entry_ts/1000)

                print(f"   ✅ MATCHED:")
                print(f"      Matched Qty: {final_qty:.6f} / {target_qty:.6f}")
                print(f"      Avg Price: {trade.entry_price:.4f} -> {weighted_avg_price:.4f}")
                print(f"      Total Cost: {trade.total_cost:.2f}€ -> {final_cost:.2f}€")
                print(f"      Safety Orders: {trade.safety_order_count} -> {so_count}")

                # Update DB
                trade.entry_price = weighted_avg_price
                trade.total_cost = final_cost
                trade.safety_order_count = so_count
                trade.entry_time = entry_dt
                trade.next_safety_order_price = weighted_avg_price * 0.98

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
