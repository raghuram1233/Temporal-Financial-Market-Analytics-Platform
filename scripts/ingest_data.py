import os
import sys
import time
import random
import requests
import yfinance as yf
from datetime import datetime, timezone, timedelta
from dotenv import load_dotenv

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
from backend.database import get_db_connection, release_db_connection

load_dotenv()

session = requests.Session()

def get_assets():
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT asset_id, symbol, type FROM assets")
            return cur.fetchall()
    finally:
        release_db_connection(conn)

def insert_market_price(asset_id, price, source='simulated'):
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO market_data (asset_id, price, time, source) VALUES (%s, %s, %s, %s)",
                (asset_id, price, datetime.now(timezone.utc), source)
            )
            conn.commit()
    except Exception as e:
        print(f"Error inserting price: {e}")
    finally:
        release_db_connection(conn)

def insert_market_price_historical(asset_id, price, timestamp, source):
    """Helper to insert historical data with specific timestamps."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Ensure timestamp is offset-aware
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
                
            cur.execute(
                "INSERT INTO market_data (asset_id, price, time, source) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                (asset_id, price, timestamp, source)
            )
            conn.commit()
    except Exception as e:
        pass # Ignore duplicates if re-running
    finally:
        release_db_connection(conn)

def has_historical_data(asset_id, days=365):
    """Check if the database already has enough historical data for this asset."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # Check for the oldest record for this asset
            cur.execute(
                "SELECT MIN(time) FROM market_data WHERE asset_id = %s",
                (asset_id,)
            )
            oldest_record = cur.fetchone()[0]
            if not oldest_record:
                return False
            
            # Ensure we are comparing offset-aware datetimes
            now = datetime.now(timezone.utc)
            if oldest_record.tzinfo is None:
                oldest_record = oldest_record.replace(tzinfo=timezone.utc)
            
            time_diff = now - oldest_record
            return time_diff.days >= (days * 0.95)
    finally:
        release_db_connection(conn)

def refresh_aggregates():
    """Manually refresh continuous aggregates to show historical data immediately."""
    conn = get_db_connection()
    try:
        with conn.cursor() as cur:
            # This ensures the charts work immediately after backfill
            cur.execute("CALL refresh_continuous_aggregate('market_data_daily', NULL, NULL);")
            conn.commit()
            print("✅ Aggregates Refreshed Successfully!")
    except Exception as e:
        print(f"⚠️ Could not refresh aggregates: {e}")
    finally:
        release_db_connection(conn)

def backfill_historical_data(assets):
    """Backfill 1 year of data: 30 days REAL from API + 335 days GENERATED."""
    total_days = 365
    real_days = 30
    sim_days = total_days - real_days
    
    print(f"⌛ Checking for historical data coverage ({total_days} days)...")
    
    missing_assets = []
    for asset in assets:
        if not has_historical_data(asset[0], total_days):
            missing_assets.append(asset)
    
    if not missing_assets:
        print("✅ Database already has 1-year history. Skipping backfill.")
        refresh_aggregates()
        return

    print(f"⌛ Starting hybrid backfill for {len(missing_assets)} assets...")
    print(f"   (30 days REAL data + {sim_days} days GENERATED data)")
    print("-" * 50)
    
    for asset_id, symbol, asset_type in missing_assets:
        try:
            print(f"🔄 Processing {symbol}...")
            
            # 1. Fetch 30 days of REAL data
            ticker_symbol = f"{symbol}-USD" if asset_type == 'crypto' else symbol
            ticker_obj = yf.Ticker(ticker_symbol)
            real_hist = ticker_obj.history(period=f"{real_days}d")
            
            first_real_price = None
            if not real_hist.empty:
                for timestamp, row in real_hist.iterrows():
                    price = float(row['Close'])
                    if first_real_price is None: first_real_price = price
                    dt = timestamp.to_pydatetime()
                    if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
                    insert_market_price_historical(asset_id, price, dt, 'real_api')
            
            # 2. Generate remaining days of SIMULATED data
            # Use the first real price as a base to work backwards
            base_price = first_real_price if first_real_price else 100.0
            current_sim_price = base_price
            
            # Start from 31 days ago and go back to 365 days ago
            start_date = datetime.now(timezone.utc) - timedelta(days=real_days + 1)
            
            for i in range(sim_days):
                sim_time = start_date - timedelta(days=i)
                # Random walk: small percentage change
                change_pct = random.uniform(-0.03, 0.03)
                current_sim_price = current_sim_price * (1 - change_pct) # Work backwards
                if current_sim_price <= 0: current_sim_price = 1.0
                
                insert_market_price_historical(asset_id, current_sim_price, sim_time, 'simulated_backfill')
            
            print(f"✅ Completed hybrid history for {symbol}")
            time.sleep(0.2) # Light delay
            
        except Exception as e:
            print(f"❌ Error backfilling {symbol}: {e}")
    
    print("-" * 50)
    print("🚀 Hybrid Backfill Complete! Refreshing aggregates...")
    refresh_aggregates()

def fetch_market_prices(assets, interval=5):
    """Fetch real-time data from Binance (Crypto) and yfinance (Stocks)."""
    print(f"🚀 UPGRADED: Real-time Data Ingestion Active (Interval: {interval}s)")
    print("-" * 50)
    
    while True:
        for asset_id, symbol, asset_type in assets:
            try:
                if asset_type == 'crypto':
                    # Use the global session for real-time to reuse ports
                    ticker = f"{symbol}USDT"
                    url = f"https://api.binance.com/api/v3/ticker/price?symbol={ticker}"
                    try:
                        response = session.get(url, timeout=5)
                        data = response.json()
                        price = float(data['price'])
                        source = 'Binance'
                    except Exception:
                        # Fallback to yfinance for real-time if Binance fails
                        ticker_obj = yf.Ticker(f"{symbol}-USD")
                        price = ticker_obj.fast_info['last_price']
                        source = 'yfinance'
                else:
                    ticker_obj = yf.Ticker(symbol)
                    price = ticker_obj.fast_info['last_price']
                    source = 'yfinance'
                
                insert_market_price(asset_id, price, source.lower())
                print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] {symbol}: ₹{price:,.2f} ({source})")
                
            except Exception as e:
                print(f"❌ Error fetching {symbol}: {e}")

        time.sleep(interval)

if __name__ == "__main__":
    print("Connecting to database...")
    try:
        assets = get_assets()
        if not assets:
            print("⚠️ No assets found. Please run schema.sql first.")
        else:
            # Step 1: Hybrid backfill (30 days real + 335 days simulated)
            backfill_historical_data(assets)
            
            # Step 2: Start continuous real-time ingestion
            fetch_market_prices(assets, interval=5)
    except Exception as e:
        print(f"CRITICAL ERROR: {e}")