-- ========================= 
-- 0. EXTENSIONS & CLEANUP (FOR RE-RUNNABILITY)
-- ========================= 
CREATE EXTENSION IF NOT EXISTS timescaledb CASCADE;
CREATE EXTENSION IF NOT EXISTS citext;

-- Drop dependent objects first
DROP VIEW IF EXISTS portfolio_summary CASCADE;
DROP VIEW IF EXISTS latest_prices CASCADE;
DROP MATERIALIZED VIEW IF EXISTS market_data_daily CASCADE;

-- Drop tables
DROP TABLE IF EXISTS audit_logs CASCADE;
DROP TABLE IF EXISTS market_data CASCADE;
DROP TABLE IF EXISTS realized_pnl CASCADE;
DROP TABLE IF EXISTS trades CASCADE;
DROP TABLE IF EXISTS orders CASCADE;
DROP TABLE IF EXISTS portfolio CASCADE;
DROP TABLE IF EXISTS wallets CASCADE;
DROP TABLE IF EXISTS assets CASCADE;
DROP TABLE IF EXISTS users CASCADE;
DROP TABLE IF EXISTS latest_prices_cache CASCADE;
DROP FUNCTION IF EXISTS process_limit_orders(INT) CASCADE;
DROP FUNCTION IF EXISTS expire_stale_orders() CASCADE;

-- ========================= 
-- 1. USERS 
-- ========================= 
CREATE TABLE users (
    user_id SERIAL PRIMARY KEY, 
    username CITEXT UNIQUE NOT NULL,
    email CITEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL, 
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP 
); 

CREATE INDEX idx_users_email ON users(email); 

-- ========================= 
-- 2. WALLETS 
-- ========================= 
CREATE TABLE wallets ( 
    wallet_id SERIAL PRIMARY KEY, 
    user_id INT UNIQUE REFERENCES users(user_id) ON DELETE CASCADE, 
    balance NUMERIC(15,2) DEFAULT 100000.00 CHECK (balance >= 0), 
    updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP 
); 

CREATE INDEX idx_wallet_user ON wallets(user_id); 

CREATE OR REPLACE FUNCTION fn_touch_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at := CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_wallet_touch_updated_at
BEFORE UPDATE ON wallets
FOR EACH ROW
EXECUTE FUNCTION fn_touch_updated_at();

-- Trigger: Auto-create wallet after user signup
CREATE OR REPLACE FUNCTION fn_create_wallet() 
RETURNS TRIGGER AS $$ 
BEGIN 
    INSERT INTO wallets(user_id) VALUES (NEW.user_id); 
    RETURN NEW; 
END; 
$$ LANGUAGE plpgsql; 

CREATE TRIGGER trg_create_wallet 
AFTER INSERT ON users 
FOR EACH ROW 
EXECUTE FUNCTION fn_create_wallet(); 

-- ========================= 
-- 3. AUDIT LOGGING (DBMS MASTER FEATURE)
-- ========================= 
CREATE TABLE audit_logs (
    log_id SERIAL PRIMARY KEY,
    user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
    action TEXT NOT NULL,
    old_value NUMERIC(15,2),
    new_value NUMERIC(15,2),
    context JSONB DEFAULT '{}'::jsonb,
    timestamp TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_audit_user_time ON audit_logs(user_id, timestamp DESC);

CREATE OR REPLACE FUNCTION fn_audit_wallet_changes() 
RETURNS TRIGGER AS $$
BEGIN
    IF (OLD.balance IS DISTINCT FROM NEW.balance) THEN
        INSERT INTO audit_logs(user_id, action, old_value, new_value)
        VALUES (NEW.user_id, 'WALLET_BALANCE_UPDATE', OLD.balance, NEW.balance);
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_audit_wallet_changes
AFTER UPDATE ON wallets
FOR EACH ROW EXECUTE FUNCTION fn_audit_wallet_changes();

-- ========================= 
-- 4. ASSETS 
-- ========================= 
CREATE TABLE assets (
    asset_id SERIAL PRIMARY KEY, 
    name TEXT NOT NULL, 
    symbol CITEXT UNIQUE NOT NULL,
    type TEXT CHECK (type IN ('crypto','stock')) NOT NULL 
); 

INSERT INTO assets (name, symbol, type) VALUES 
('Bitcoin', 'BTC', 'crypto'),
('Ethereum', 'ETH', 'crypto'),
('Solana', 'SOL', 'crypto'),
('Cardano', 'ADA', 'crypto'),
('Ripple', 'XRP', 'crypto'),
('Dogecoin', 'DOGE', 'crypto'),
('Polkadot', 'DOT', 'crypto'),
('Chainlink', 'LINK', 'crypto'),
('Polygon', 'MATIC', 'crypto'),
('Avalanche', 'AVAX', 'crypto'),
('Litecoin', 'LTC', 'crypto'),
('Shiba Inu', 'SHIB', 'crypto'),
('Apple Inc.', 'AAPL', 'stock'),
('Tesla Inc.', 'TSLA', 'stock'),
('NVIDIA Corp.', 'NVDA', 'stock'),
('Microsoft Corp.', 'MSFT', 'stock'),
('Amazon.com Inc.', 'AMZN', 'stock'),
('Alphabet Inc. (Google)', 'GOOGL', 'stock'),
('Meta Platforms Inc. (Facebook)', 'META', 'stock'),
('Netflix Inc.', 'NFLX', 'stock'),
('Walt Disney Co.', 'DIS', 'stock'),
('Adobe Inc.', 'ADBE', 'stock'),
('Intel Corp.', 'INTC', 'stock'),
('Advanced Micro Devices Inc. (AMD)', 'AMD', 'stock'),
('Salesforce Inc.', 'CRM', 'stock');

-- ========================= 
-- 5. MARKET DATA & LATEST PRICES CACHE
-- ========================= 
CREATE TABLE market_data ( 
    asset_id INT REFERENCES assets(asset_id) ON DELETE CASCADE, 
    price NUMERIC(18,8) NOT NULL CHECK (price > 0), 
    time TIMESTAMPTZ NOT NULL, 
    source TEXT DEFAULT 'simulated'
); 

SELECT create_hypertable('market_data', 'time'); 
ALTER TABLE market_data SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'asset_id'
);
SELECT add_compression_policy('market_data', INTERVAL '7 days');

-- Continuous Aggregate for Daily OHLC (Candlesticks)
CREATE MATERIALIZED VIEW market_data_daily
WITH (timescaledb.continuous) AS
SELECT
  time_bucket('1 day', time) AS bucket,
  asset_id,
  MIN(price) AS low,
  MAX(price) AS high,
  FIRST(price, time) AS open,
  LAST(price, time) AS close
FROM market_data
GROUP BY bucket, asset_id
WITH NO DATA;

-- Policy to refresh the aggregate daily
SELECT add_continuous_aggregate_policy('market_data_daily',
  start_offset => INTERVAL '1 month',
  end_offset => INTERVAL '1 hour',
  schedule_interval => INTERVAL '1 day');

CREATE INDEX idx_market_asset_time ON market_data (asset_id, time DESC); 
CREATE INDEX idx_market_time ON market_data(time DESC); 

-- Cache table for ultra-fast latest price lookups
CREATE TABLE latest_prices_cache (
    asset_id INT PRIMARY KEY REFERENCES assets(asset_id) ON DELETE CASCADE,
    price NUMERIC(18,8) NOT NULL,
    time TIMESTAMPTZ NOT NULL
);

-- Trigger to keep cache updated
CREATE OR REPLACE FUNCTION fn_update_latest_price_cache()
RETURNS TRIGGER AS $$
BEGIN
    INSERT INTO latest_prices_cache (asset_id, price, time)
    VALUES (NEW.asset_id, NEW.price, NEW.time)
    ON CONFLICT (asset_id) DO UPDATE
    SET price = EXCLUDED.price,
        time = EXCLUDED.time
    WHERE latest_prices_cache.time <= EXCLUDED.time;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_update_latest_price_cache
AFTER INSERT ON market_data
FOR EACH ROW EXECUTE FUNCTION fn_update_latest_price_cache();

-- Optimized View: Latest Prices (now uses cache)
CREATE OR REPLACE VIEW latest_prices AS 
SELECT asset_id, price, time FROM latest_prices_cache;

-- One-time population of the cache
INSERT INTO latest_prices_cache (asset_id, price, time)
SELECT DISTINCT ON (asset_id) asset_id, price, time
FROM market_data
ORDER BY asset_id, time DESC
ON CONFLICT (asset_id) DO NOTHING;

-- ========================= 
-- 6. ORDERS & TRADES 
-- ========================= 
CREATE TABLE orders ( 
    order_id SERIAL, 
    user_id INT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE, 
    asset_id INT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE, 
    quantity NUMERIC(15,5) NOT NULL CHECK (quantity > 0), 
    price NUMERIC(15,5) NOT NULL CHECK (price > 0), 
    order_type TEXT CHECK (order_type IN ('buy', 'sell')) NOT NULL, 
    order_kind TEXT NOT NULL DEFAULT 'market' CHECK (order_kind IN ('market', 'limit', 'stop_loss')),
    target_price NUMERIC(15,5),
    expires_at TIMESTAMPTZ,
    status TEXT CHECK (status IN ('open', 'filled', 'cancelled')) DEFAULT 'open', 
    created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (order_id, created_at)
); 

SELECT create_hypertable('orders', 'created_at');
CREATE INDEX idx_orders_user_created_at ON orders(user_id, created_at DESC);
CREATE INDEX idx_orders_user_status_created_at ON orders(user_id, status, created_at DESC);
CREATE INDEX idx_orders_open_asset_kind_target ON orders(asset_id, order_kind, status, target_price);

CREATE TABLE trades( 
    trade_id SERIAL, 
    order_id INT, 
    user_id INT NOT NULL REFERENCES users(user_id), 
    asset_id INT NOT NULL REFERENCES assets(asset_id), 
    trade_type TEXT NOT NULL CHECK (trade_type IN ('buy','sell')), 
    price NUMERIC(15,5) NOT NULL CHECK (price > 0), 
    quantity NUMERIC(15,5) NOT NULL CHECK (quantity > 0), 
    executed_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (trade_id, executed_at)
); 

SELECT create_hypertable('trades', 'executed_at');
CREATE INDEX idx_trades_user_executed_at ON trades(user_id, executed_at DESC);
CREATE INDEX idx_trades_asset_executed_at ON trades(asset_id, executed_at DESC);

-- ========================= 
-- 7. PORTFOLIO 
-- ========================= 
CREATE TABLE portfolio ( 
    portfolio_version_id BIGSERIAL PRIMARY KEY,
    user_id INT REFERENCES users(user_id) ON DELETE CASCADE, 
    asset_id INT REFERENCES assets(asset_id) ON DELETE CASCADE, 
    quantity NUMERIC(15,5) DEFAULT 0 CHECK (quantity >= 0), 
    avg_price NUMERIC(15,5) DEFAULT 0 CHECK (avg_price >= 0),
    valid_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    valid_to TIMESTAMPTZ,
    transaction_from TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    transaction_to TIMESTAMPTZ
); 

CREATE INDEX idx_portfolio_user ON portfolio(user_id);
CREATE INDEX idx_portfolio_current ON portfolio(user_id, asset_id) WHERE valid_to IS NULL AND transaction_to IS NULL;
CREATE UNIQUE INDEX idx_portfolio_unique_current ON portfolio(user_id, asset_id) WHERE valid_to IS NULL AND transaction_to IS NULL;

CREATE TABLE realized_pnl (
    time TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    user_id INT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    asset_id INT NOT NULL REFERENCES assets(asset_id) ON DELETE CASCADE,
    quantity NUMERIC(15,5) NOT NULL,
    buy_avg_price NUMERIC(15,5) NOT NULL,
    sell_price NUMERIC(15,5) NOT NULL,
    realized_profit NUMERIC(18,5) NOT NULL
);

SELECT create_hypertable('realized_pnl', 'time');
CREATE INDEX idx_realized_pnl_user_time ON realized_pnl(user_id, time DESC);

-- ========================= 
-- 8. TRIGGERS FOR TRADES
-- ========================= 

-- Trigger: Update wallet after trade
CREATE OR REPLACE FUNCTION fn_update_wallet_after_trade() 
RETURNS TRIGGER AS $$ 
DECLARE
    v_current_avg NUMERIC;
    v_realized_pnl NUMERIC;
BEGIN 
    IF NEW.trade_type = 'buy' THEN
        UPDATE wallets SET balance = balance - (NEW.price * NEW.quantity) WHERE user_id = NEW.user_id;
    ELSE
        -- Realized P&L = (Selling Price - Original Purchase Price) * Quantity Sold
        -- We get the original purchase price (avg_price) from the current portfolio version
        SELECT avg_price INTO v_current_avg
        FROM portfolio
        WHERE user_id = NEW.user_id
          AND asset_id = NEW.asset_id
          AND valid_to IS NULL
          AND transaction_to IS NULL;

        v_realized_pnl := (NEW.price - COALESCE(v_current_avg, NEW.price)) * NEW.quantity;

        -- Update balance: Add the full selling amount (already done by the simple logic)
        -- The user specifically asked: "This money is now sitting in the user's cash balance"
        -- Selling an asset already adds (price * quantity) to the balance.
        -- Realized P&L is a *subset* of that total cash flow that represents profit.
        UPDATE wallets SET balance = balance + (NEW.price * NEW.quantity) WHERE user_id = NEW.user_id;
        
        -- Optional: Log the realized P&L specifically to audit logs for transparency
        INSERT INTO audit_logs(user_id, action, old_value, new_value, context)
        VALUES (NEW.user_id, 'REALIZED_PNL_BOOKED', 0, v_realized_pnl, jsonb_build_object('asset_id', NEW.asset_id, 'trade_id', NEW.trade_id));
    END IF;
    RETURN NEW; 
END; 
$$ LANGUAGE plpgsql; 

CREATE TRIGGER trg_update_wallet_after_trade 
AFTER INSERT ON trades 
FOR EACH ROW EXECUTE FUNCTION fn_update_wallet_after_trade();

-- Trigger: Update portfolio after trade
CREATE OR REPLACE FUNCTION fn_update_portfolio_after_trade() 
RETURNS TRIGGER AS $$ 
DECLARE
    v_current_qty NUMERIC;
    v_current_avg NUMERIC;
    v_new_qty NUMERIC;
    v_new_avg NUMERIC;
    v_realized NUMERIC;
BEGIN 
    IF NEW.trade_type = 'buy' THEN
        SELECT quantity, avg_price INTO v_current_qty, v_current_avg
        FROM portfolio
        WHERE user_id = NEW.user_id
          AND asset_id = NEW.asset_id
          AND valid_to IS NULL
          AND transaction_to IS NULL
        FOR UPDATE;

        IF v_current_qty IS NULL THEN
            INSERT INTO portfolio(user_id, asset_id, quantity, avg_price, valid_from, valid_to, transaction_from, transaction_to)
            VALUES (NEW.user_id, NEW.asset_id, NEW.quantity, NEW.price, NOW(), NULL, NOW(), NULL);
        ELSE
            v_new_qty := v_current_qty + NEW.quantity;
            v_new_avg := ((v_current_avg * v_current_qty) + (NEW.price * NEW.quantity)) / NULLIF(v_new_qty, 0);

            UPDATE portfolio
            SET valid_to = NOW(), transaction_to = NOW()
            WHERE user_id = NEW.user_id
              AND asset_id = NEW.asset_id
              AND valid_to IS NULL
              AND transaction_to IS NULL;

            INSERT INTO portfolio(user_id, asset_id, quantity, avg_price, valid_from, valid_to, transaction_from, transaction_to)
            VALUES (NEW.user_id, NEW.asset_id, v_new_qty, COALESCE(v_new_avg, NEW.price), NOW(), NULL, NOW(), NULL);
        END IF;
    ELSE
        SELECT quantity, avg_price INTO v_current_qty, v_current_avg
        FROM portfolio
        WHERE user_id = NEW.user_id
          AND asset_id = NEW.asset_id
          AND valid_to IS NULL
          AND transaction_to IS NULL
        FOR UPDATE;

        IF v_current_qty IS NULL OR v_current_qty < NEW.quantity THEN
            RAISE EXCEPTION 'Insufficient holdings for user % on asset %', NEW.user_id, NEW.asset_id;
        END IF;

        v_new_qty := v_current_qty - NEW.quantity;
        v_realized := (NEW.price - v_current_avg) * NEW.quantity;

        INSERT INTO realized_pnl(time, user_id, asset_id, quantity, buy_avg_price, sell_price, realized_profit)
        VALUES (NOW(), NEW.user_id, NEW.asset_id, NEW.quantity, v_current_avg, NEW.price, v_realized);

        UPDATE portfolio
        SET valid_to = NOW(), transaction_to = NOW()
        WHERE user_id = NEW.user_id
          AND asset_id = NEW.asset_id
          AND valid_to IS NULL
          AND transaction_to IS NULL;

        IF v_new_qty > 0 THEN
            INSERT INTO portfolio(user_id, asset_id, quantity, avg_price, valid_from, valid_to, transaction_from, transaction_to)
            VALUES (NEW.user_id, NEW.asset_id, v_new_qty, v_current_avg, NOW(), NULL, NOW(), NULL);
        END IF;
    END IF;
    RETURN NEW; 
END; 
$$ LANGUAGE plpgsql; 

CREATE TRIGGER trg_update_portfolio_after_trade 
AFTER INSERT ON trades 
FOR EACH ROW EXECUTE FUNCTION fn_update_portfolio_after_trade();

-- ========================= 
-- 9. CORE FUNCTIONS
-- ========================= 

CREATE OR REPLACE FUNCTION execute_trade(p_order_id INT)
RETURNS VOID AS $$
DECLARE
    v_user INT; v_asset INT; v_type TEXT; v_price NUMERIC; v_qty NUMERIC; v_kind TEXT;
    v_total_cost NUMERIC; v_balance NUMERIC; v_holdings NUMERIC;
BEGIN
    SELECT user_id, asset_id, order_type, price, quantity, order_kind
    INTO v_user, v_asset, v_type, v_price, v_qty, v_kind
    FROM orders
    WHERE order_id = p_order_id
      AND created_at >= NOW() - INTERVAL '7 days'
    LIMIT 1;

    IF v_user IS NULL THEN
        SELECT user_id, asset_id, order_type, price, quantity, order_kind
        INTO v_user, v_asset, v_type, v_price, v_qty, v_kind
        FROM orders
        WHERE order_id = p_order_id
        LIMIT 1;
    END IF;

    IF v_user IS NULL THEN
        RAISE EXCEPTION 'Order % not found', p_order_id;
    END IF;

    v_total_cost := v_price * v_qty;

    IF v_type = 'buy' THEN
        SELECT balance INTO v_balance FROM wallets WHERE user_id = v_user FOR UPDATE;
        IF v_balance < v_total_cost THEN
            UPDATE orders SET status = 'cancelled' WHERE order_id = p_order_id;
            RAISE EXCEPTION 'Insufficient balance';
        END IF;
    ELSE
        SELECT quantity INTO v_holdings
        FROM portfolio
        WHERE user_id = v_user
          AND asset_id = v_asset
          AND valid_to IS NULL
          AND transaction_to IS NULL
        FOR UPDATE;
        IF v_holdings IS NULL OR v_holdings < v_qty THEN
            UPDATE orders SET status = 'cancelled' WHERE order_id = p_order_id;
            RAISE EXCEPTION 'Insufficient assets';
        END IF;
    END IF;

    INSERT INTO trades(order_id, user_id, asset_id, price, quantity, trade_type)
    VALUES (p_order_id, v_user, v_asset, v_price, v_qty, v_type);

    UPDATE orders SET status = 'filled' WHERE order_id = p_order_id;
END;
$$ LANGUAGE plpgsql; 

CREATE OR REPLACE FUNCTION place_order(p_user INT, p_asset INT, p_type TEXT, p_qty NUMERIC) 
RETURNS INT AS $$ 
DECLARE v_price NUMERIC; v_order_id INT;
BEGIN 
    RETURN place_order(p_user, p_asset, p_type, p_qty, 'market', NULL, NULL);
END; 
$$ LANGUAGE plpgsql; 

CREATE OR REPLACE FUNCTION place_order(
    p_user INT,
    p_asset INT,
    p_type TEXT,
    p_qty NUMERIC,
    p_order_kind TEXT DEFAULT 'market',
    p_target_price NUMERIC DEFAULT NULL,
    p_expires_at TIMESTAMPTZ DEFAULT NULL
) RETURNS INT AS $$
DECLARE
    v_price NUMERIC;
    v_order_id INT;
    v_kind TEXT;
BEGIN
    IF p_qty IS NULL OR p_qty <= 0 THEN
        RAISE EXCEPTION 'Quantity must be greater than zero';
    END IF;

    IF LOWER(p_type) NOT IN ('buy', 'sell') THEN
        RAISE EXCEPTION 'Invalid order type';
    END IF;

    v_kind := LOWER(COALESCE(p_order_kind, 'market'));
    IF v_kind NOT IN ('market', 'limit', 'stop_loss') THEN
        RAISE EXCEPTION 'Invalid order kind';
    END IF;

    SELECT price INTO v_price FROM latest_prices WHERE asset_id = p_asset;
    IF v_price IS NULL THEN
        RAISE EXCEPTION 'No price data available';
    END IF;

    IF v_kind IN ('limit', 'stop_loss') AND (p_target_price IS NULL OR p_target_price <= 0) THEN
        RAISE EXCEPTION 'Target price required for limit/stop_loss orders';
    END IF;

    INSERT INTO orders(user_id, asset_id, quantity, price, order_type, order_kind, target_price, expires_at, status)
    VALUES (
        p_user,
        p_asset,
        p_qty,
        COALESCE(p_target_price, v_price),
        LOWER(p_type),
        v_kind,
        p_target_price,
        p_expires_at,
        'open'
    )
    RETURNING order_id INTO v_order_id;

    IF v_kind = 'market' THEN
        PERFORM execute_trade(v_order_id);
    END IF;

    RETURN v_order_id;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION process_limit_orders(p_asset_id INT)
RETURNS INT AS $$
DECLARE
    v_latest_price NUMERIC;
    v_processed INT := 0;
    v_order RECORD;
BEGIN
    SELECT price INTO v_latest_price FROM latest_prices WHERE asset_id = p_asset_id;
    IF v_latest_price IS NULL THEN
        RETURN 0;
    END IF;

    FOR v_order IN
        SELECT order_id, order_type, order_kind, target_price
        FROM orders
        WHERE asset_id = p_asset_id
          AND status = 'open'
          AND order_kind IN ('limit', 'stop_loss')
          AND (expires_at IS NULL OR expires_at > NOW())
        ORDER BY created_at ASC
    LOOP
        IF (
            v_order.order_kind = 'limit' AND (
                (v_order.order_type = 'buy' AND v_latest_price <= v_order.target_price) OR
                (v_order.order_type = 'sell' AND v_latest_price >= v_order.target_price)
            )
        ) OR (
            v_order.order_kind = 'stop_loss' AND (
                (v_order.order_type = 'buy' AND v_latest_price >= v_order.target_price) OR
                (v_order.order_type = 'sell' AND v_latest_price <= v_order.target_price)
            )
        ) THEN
            UPDATE orders SET price = v_latest_price WHERE order_id = v_order.order_id;
            PERFORM execute_trade(v_order.order_id);
            v_processed := v_processed + 1;
        END IF;
    END LOOP;

    RETURN v_processed;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION expire_stale_orders()
RETURNS INT AS $$
DECLARE
    v_count INT;
BEGIN
    UPDATE orders
    SET status = 'cancelled'
    WHERE status = 'open'
      AND expires_at IS NOT NULL
      AND expires_at <= NOW();

    GET DIAGNOSTICS v_count = ROW_COUNT;
    RETURN v_count;
END;
$$ LANGUAGE plpgsql;

-- ========================= 
-- 10. VIEWS
-- ========================= 

-- View: Portfolio Summary
CREATE VIEW portfolio_summary AS 
SELECT 
    p.user_id, p.asset_id, a.symbol, p.quantity, p.avg_price, 
    COALESCE(lp.price, 0) as current_price,
    (COALESCE(lp.price, 0) * p.quantity) as current_value, 
    (COALESCE(lp.price, 0) - p.avg_price) * p.quantity as unrealized_pl
FROM portfolio p 
JOIN assets a ON p.asset_id = a.asset_id
LEFT JOIN latest_prices lp ON p.asset_id = lp.asset_id
WHERE p.valid_to IS NULL
  AND p.transaction_to IS NULL; 

CREATE OR REPLACE FUNCTION deposit_money(p_user_id INT, p_amount NUMERIC) RETURNS VOID AS $$
BEGIN
IF p_amount IS NULL OR p_amount <= 0 THEN RAISE EXCEPTION 'Deposit amount must be greater than zero'; END IF;
UPDATE wallets SET balance = balance + p_amount WHERE user_id = p_user_id;
END; $$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION withdraw_money(p_user_id INT, p_amount NUMERIC) RETURNS VOID AS $$
BEGIN
IF p_amount IS NULL OR p_amount <= 0 THEN RAISE EXCEPTION 'Withdrawal amount must be greater than zero'; END IF;
UPDATE wallets SET balance = balance - p_amount WHERE user_id = p_user_id AND balance >= p_amount;
IF NOT FOUND THEN RAISE EXCEPTION 'Insufficient balance'; END IF; END; $$ LANGUAGE plpgsql;