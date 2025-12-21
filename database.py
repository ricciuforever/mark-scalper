import os
import json
from datetime import datetime
from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime, Boolean, func
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

class Settings(Base):
    __tablename__ = 'settings'
    key = Column(String, primary_key=True)
    value = Column(String)  # Stored as string, cast when reading

class ActiveTrade(Base):
    __tablename__ = 'active_trades'
    symbol = Column(String, primary_key=True)
    entry_price = Column(Float)
    quantity = Column(Float)
    current_price = Column(Float, default=0.0)
    safety_order_count = Column(Integer, default=0)
    highest_price = Column(Float, default=0.0)
    entry_time = Column(DateTime, default=datetime.utcnow)
    is_dust = Column(Boolean, default=False)
    last_error = Column(String, nullable=True)

    # New fields for DCA Strategy
    base_order_price = Column(Float, default=0.0) # Price of the initial order
    total_cost = Column(Float, default=0.0)       # Total EUR spent
    next_safety_order_price = Column(Float, default=0.0)

class TradeHistory(Base):
    __tablename__ = 'trade_history'
    id = Column(Integer, primary_key=True, autoincrement=True)
    symbol = Column(String)
    entry_price = Column(Float)
    exit_price = Column(Float)
    quantity = Column(Float)
    pnl_abs = Column(Float)
    pnl_pct = Column(Float)
    reason = Column(String)
    close_time = Column(DateTime, default=datetime.utcnow)

class DatabaseManager:
    def __init__(self, db_path='trades.db'):
        self.engine = create_engine(f'sqlite:///{db_path}', echo=False)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

    def get_session(self):
        return self.Session()

    def get_setting(self, key, default=None, type_cast=str):
        session = self.Session()
        try:
            setting = session.query(Settings).filter_by(key=key).first()
            if setting:
                if type_cast == bool:
                    return setting.value.lower() == 'true'
                return type_cast(setting.value)
            return default
        finally:
            session.close()

    def set_setting(self, key, value):
        session = self.Session()
        try:
            setting = session.query(Settings).filter_by(key=key).first()
            if not setting:
                setting = Settings(key=key)
                session.add(setting)
            setting.value = str(value)
            session.commit()
        except Exception as e:
            session.rollback()
            raise e
        finally:
            session.close()

    def migrate_from_json(self):
        """Migrates data from active_trades.json and trades.json to SQLite."""
        session = self.Session()
        try:
            # 1. Migrate Active Trades
            if os.path.exists('active_trades.json'):
                with open('active_trades.json', 'r') as f:
                    data = json.load(f)
                    for sym, trade in data.items():
                        existing = session.query(ActiveTrade).filter_by(symbol=sym).first()
                        if not existing:
                            # Parse entry_time
                            et = trade.get('entry_time')
                            if isinstance(et, str):
                                try:
                                    et = datetime.fromisoformat(et)
                                except:
                                    et = datetime.utcnow()

                            new_trade = ActiveTrade(
                                symbol=sym,
                                entry_price=trade.get('entry_price', 0.0),
                                quantity=trade.get('quantity', 0.0),
                                current_price=trade.get('current_price', 0.0),
                                safety_order_count=int(trade.get('safety_order_count', 0)),
                                highest_price=trade.get('highest_price', trade.get('entry_price', 0.0)),
                                entry_time=et,
                                is_dust=trade.get('is_dust', False),
                                last_error=trade.get('last_error'),
                                base_order_price=trade.get('entry_price', 0.0),
                                total_cost=trade.get('entry_price', 0.0) * trade.get('quantity', 0.0)
                            )
                            # Calculate next safety order price for migrated trades (Default 2% drop)
                            new_trade.next_safety_order_price = new_trade.entry_price * 0.98

                            session.add(new_trade)

            # 2. Migrate Trade History
            if os.path.exists('trades.json'):
                with open('trades.json', 'r') as f:
                    data = json.load(f)
                    # trades.json is a list
                    for trade in data:
                        # Check for duplicates might be hard without unique ID,
                        # but we can check symbol + close_time
                        ct_str = trade.get('close_time')
                        try:
                            ct = datetime.strptime(ct_str, "%Y-%m-%d %H:%M:%S")
                        except:
                            ct = datetime.utcnow()

                        # Simple duplicate check
                        exists = session.query(TradeHistory).filter_by(
                            symbol=trade.get('symbol'),
                            close_time=ct
                        ).first()

                        if not exists:
                            h = TradeHistory(
                                symbol=trade.get('symbol'),
                                entry_price=trade.get('entry_price', 0.0),
                                exit_price=trade.get('exit_price', 0.0),
                                quantity=trade.get('quantity', 0.0),
                                pnl_abs=trade.get('pnl_abs', 0.0),
                                pnl_pct=trade.get('pnl_pct', 0.0),
                                reason=trade.get('reason', ''),
                                close_time=ct
                            )
                            session.add(h)

            session.commit()
            print("✅ Migration to SQLite completed.")
        except Exception as e:
            session.rollback()
            print(f"❌ Migration failed: {e}")
        finally:
            session.close()
