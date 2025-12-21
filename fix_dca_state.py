import sys
from database import DatabaseManager, ActiveTrade, Settings

def fix_dca_state():
    print("--- DCA State Reconstruction Tool ---")
    db = DatabaseManager()
    session = db.get_session()

    try:
        # Load Settings
        bo_size = db.get_setting('base_order_size', 50.0, float)
        vol_scale = db.get_setting('dca_volume_scale', 1.5, float)
        step_scale = db.get_setting('dca_step_scale', 0.02, float)

        print(f"Config: Base Order={bo_size}€, Scale={vol_scale}x")

        trades = session.query(ActiveTrade).all()
        for t in trades:
            print(f"\nScanning {t.symbol}...")
            print(f"  Current DB State: Cost={t.total_cost:.2f}€, SO={t.safety_order_count}")

            # Reconstruct Logic
            # Level 0: Cost ≈ BO
            # Level 1: Cost ≈ BO + BO*Scale
            # Level 2: Cost ≈ BO + BO*Scale + BO*Scale^2

            accumulated_cost = 0.0
            estimated_so = 0

            # Check Base
            accumulated_cost += bo_size

            # If total cost is significantly higher than base, iterate
            # We use a margin of error (e.g. 10%)
            if t.total_cost > (accumulated_cost * 1.1):
                # It's at least SO 1?

                # Loop to find closest level
                # We limit to max 10 to avoid infinite loops
                for level in range(1, 10):
                    next_order_size = bo_size * (vol_scale ** level)
                    accumulated_cost += next_order_size

                    if t.total_cost <= (accumulated_cost * 1.1):
                        estimated_so = level
                        break

                    # If we exceeded the cost significantly, maybe it's this level?
                    # Actually the loop condition handles "up to".

            if estimated_so != t.safety_order_count:
                print(f"  ⚠️ Mismatch Detected! Estimated SO Level: {estimated_so}")
                t.safety_order_count = estimated_so

                # Also fix next_safety_order_price
                # We don't know the exact levels, but we can set it safely below current price
                # so it doesn't trigger immediately unless price drops further.
                # Logic: next_price = current_market_price * (1 - step_scale)
                # Note: t.current_price is updated by bot.

                # Fallback if current_price is 0
                ref_price = t.current_price if t.current_price > 0 else t.entry_price
                t.next_safety_order_price = ref_price * (1 - step_scale)

                print(f"  ✅ Fixed -> SO: {estimated_so}, Next Target: {t.next_safety_order_price:.4f}")
            else:
                print("  ✅ State appears consistent.")

        session.commit()
        print("\n--- Repair Complete ---")
        print("Please restart the bot for changes to take effect.")

    except Exception as e:
        session.rollback()
        print(f"❌ Error: {e}")
    finally:
        session.close()

if __name__ == "__main__":
    fix_dca_state()
