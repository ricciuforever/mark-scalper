import sys
from database import DatabaseManager, ActiveTrade

def set_avg_price():
    if len(sys.argv) != 3:
        print("Usage: python3 set_avg_price.py <SYMBOL> <REAL_AVG_PRICE>")
        print("Example: python3 set_avg_price.py ADAEUR 0.55")
        return

    symbol = sys.argv[1].upper()
    try:
        new_price = float(sys.argv[2])
    except ValueError:
        print("❌ Invalid price format.")
        return

    db = DatabaseManager()
    session = db.get_session()

    try:
        trade = session.query(ActiveTrade).filter_by(symbol=symbol).first()
        if not trade:
            print(f"❌ Trade for {symbol} not found in database.")
            return

        old_price = trade.entry_price
        trade.entry_price = new_price

        # Also ensure highest_price is at least this (to avoid weird trailing logic if added later)
        if trade.highest_price < new_price:
            trade.highest_price = new_price

        # We should NOT touch quantity or cost, as those are synced from wallet.
        # Changing entry price means the calculated PnL will change.

        session.commit()
        print(f"✅ Updated {symbol} Entry Price: {old_price:.4f} -> {new_price:.4f}")
        print("Restart the bot to see correct PnL.")

    except Exception as e:
        session.rollback()
        print(f"❌ Error: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    set_avg_price()
