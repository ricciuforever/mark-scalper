import json
import os
import sys
from database import DatabaseManager, ActiveTrade

def restore_trades():
    if not os.path.exists('active_trades.json'):
        print("❌ Error: active_trades.json not found. Cannot restore history.")
        return

    print("--- Restoring Trades from active_trades.json ---")

    with open('active_trades.json', 'r') as f:
        legacy_data = json.load(f)

    db = DatabaseManager()
    session = db.get_session()

    restored_count = 0

    try:
        for symbol, data in legacy_data.items():
            # Find the existing "bad" trade in DB
            trade = session.query(ActiveTrade).filter_by(symbol=symbol).first()

            if trade:
                print(f"🔄 Updating {symbol}...")

                # Restore values
                old_entry = float(data.get('entry_price', 0.0))
                old_qty = float(data.get('quantity', 0.0))

                # Safely get safety_order_count
                so_count = data.get('safety_order_count', 0)
                if so_count is None: so_count = 0

                trade.entry_price = old_entry
                trade.quantity = old_qty
                trade.safety_order_count = int(so_count)

                # Restore extra fields if available, else calculate
                if 'total_cost' in data:
                    trade.total_cost = float(data['total_cost'])
                else:
                    trade.total_cost = old_entry * old_qty

                if 'highest_price' in data:
                    trade.highest_price = float(data['highest_price'])
                else:
                    trade.highest_price = old_entry

                # Recalculate Next SO (approximate if not saved)
                # We assume 2% step if unknown, but better to check if we can calculate it
                # If we don't have it, let the bot recalculate or set a default
                # The bot logic: trade.next_safety_order_price = avg_price * (1 - step_scale)
                # We'll leave it as is or update it?
                # Better to update it based on restored entry price
                trade.next_safety_order_price = old_entry * 0.98 # Default 2% step assumption

                trade.is_dust = False # Reset dust flag if it was marked as dust

                restored_count += 1
            else:
                print(f"⚠️ {symbol} found in JSON but NOT in DB. It might have been sold or closed manually.")
                # Optional: Re-create it?
                # If the wallet sync didn't pick it up, it means it's not in the wallet.
                # So we should NOT re-create it.

        session.commit()
        print(f"✅ Successfully restored {restored_count} trades.")
        print("Please restart the bot to apply changes.")

    except Exception as e:
        session.rollback()
        print(f"❌ Error during restore: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    restore_trades()
