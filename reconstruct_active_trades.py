
import os
import sys
from datetime import datetime
from dotenv import load_dotenv
from binance.spot import Spot
from database import DatabaseManager, ActiveTrade
from collections import deque

# Load .env
load_dotenv()

def reconstruct_active_trades():
    print("--- Active Trades Reconstruction Tool (FIFO Inventory) ---")

    api_key = os.getenv('BINANCE_API_KEY')
    api_secret = os.getenv('BINANCE_API_SECRET')

    if not api_key or not api_secret:
        print("❌ Error: Missing API credentials in .env")
        return

    client = Spot(api_key=api_key, api_secret=api_secret)
    db = DatabaseManager()
    session = db.get_session()

    # User requested Start Date: Nov 1, 2025
    START_TS = 1761955200000
    print(f"📅 Analysis Start Date: {datetime.fromtimestamp(START_TS/1000)}")

    try:
        trades = session.query(ActiveTrade).all()
        if not trades:
            print("No active trades found in DB.")
            return

        for trade in trades:
            symbol = trade.symbol
            db_qty = trade.quantity
            print(f"\nScanning {symbol} (DB Qty: {db_qty})...")

            try:
                # 1. Fetch ALL trades since start date
                all_trades = []
                # Pagination loop
                fetch_ts = START_TS
                while True:
                    fetched = client.my_trades(symbol=symbol, startTime=int(fetch_ts), limit=1000)
                    if not fetched:
                        break
                    all_trades.extend(fetched)
                    if len(fetched) < 1000:
                        break
                    # Next page
                    last_time = fetched[-1]['time']
                    if last_time == fetch_ts:
                        fetch_ts += 1
                    else:
                        fetch_ts = last_time + 1

                all_trades.sort(key=lambda x: x['time']) # Oldest first

                if not all_trades:
                    print(f"   ⚠️ No trades found since start date.")
                    continue

                # 2. Simulate Inventory (FIFO)
                # Queue stores tuples: (qty, cost, orderId, time)
                inventory = deque()

                for t in all_trades:
                    qty = float(t['qty'])
                    price = float(t['price'])
                    cost = float(t['quoteQty'])
                    order_id = t['orderId']
                    ts = t['time']

                    if t['isBuyer']:
                        # Add to inventory
                        inventory.append({
                            'qty': qty,
                            'cost': cost,
                            'price': price,
                            'orderId': order_id,
                            'time': ts
                        })
                    else:
                        # SELL - Remove from inventory (FIFO)
                        qty_to_remove = qty

                        while qty_to_remove > 0.00000001 and inventory:
                            first_item = inventory[0]

                            if first_item['qty'] <= qty_to_remove:
                                # Consume entire item
                                qty_to_remove -= first_item['qty']
                                inventory.popleft()
                            else:
                                # Partial consume
                                fraction = qty_to_remove / first_item['qty']
                                first_item['qty'] -= qty_to_remove
                                first_item['cost'] -= (first_item['cost'] * fraction) # Reduce cost proportionally
                                qty_to_remove = 0

                # 3. Analyze Remaining Inventory
                final_qty = sum(item['qty'] for item in inventory)
                final_cost = sum(item['cost'] for item in inventory)

                print(f"   Calculated Inventory: {final_qty:.6f} (DB says {db_qty:.6f})")

                # Validation
                if final_qty < (db_qty * 0.9):
                    print(f"   ⚠️ Mismatch: Calculated quantity is significantly lower than DB.")
                    print("      This implies some coins were bought BEFORE Nov 1, 2025.")
                    print("      Skipping update to avoid breaking cost basis.")
                    continue

                if final_qty == 0:
                     print("   ⚠️ Calculated inventory is 0. Trade might be closed?")
                     continue

                # 4. Calculate Stats
                weighted_avg_price = final_cost / final_qty

                # Count distinct BUY Orders (Safety Orders)
                # We look at the 'orderId's present in the inventory
                unique_order_ids = set(item['orderId'] for item in inventory)
                total_orders = len(unique_order_ids)

                # Safety Order Count = Total Orders - 1 (Base Order)
                so_count = max(0, total_orders - 1)

                # Find Entry Time (Time of the oldest order in inventory)
                if inventory:
                    entry_ts = inventory[0]['time']
                    entry_dt = datetime.fromtimestamp(entry_ts/1000)
                else:
                    entry_dt = datetime.utcnow()

                # 5. Update DB
                print(f"   ✅ RECONSTRUCTED:")
                print(f"      Avg Price: {trade.entry_price:.4f} -> {weighted_avg_price:.4f}")
                print(f"      Total Cost: {trade.total_cost:.2f}€ -> {final_cost:.2f}€")
                print(f"      Safety Orders: {trade.safety_order_count} -> {so_count}")

                trade.entry_price = weighted_avg_price
                trade.total_cost = final_cost
                trade.safety_order_count = so_count
                trade.entry_time = entry_dt

                # Update Next SO Target based on new Avg
                # Default 2% step
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
