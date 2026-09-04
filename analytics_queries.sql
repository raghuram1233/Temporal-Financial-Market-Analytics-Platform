-- ==========================================
-- TEMPORAL ANALYTICS QUERIES (WINDOW FUNCTIONS)
-- ==========================================

-- 1. Moving Average (7-period simple moving average)
SELECT 
    asset_id,
    time,
    price,
    AVG(price) OVER (PARTITION BY asset_id ORDER BY time ROWS BETWEEN 6 PRECEDING AND CURRENT ROW) AS sma_7
FROM market_data;

-- 2. Price Change using LAG
WITH price_with_lag AS (
    SELECT
        asset_id,
        time,
        price,
        LAG(price) OVER (PARTITION BY asset_id ORDER BY time) AS prev_price
    FROM market_data
)
SELECT
    asset_id,
    time,
    price,
    prev_price,
    CASE
        WHEN prev_price IS NOT NULL AND prev_price <> 0
        THEN (price - prev_price) / prev_price * 100
        ELSE NULL
    END AS pct_change
FROM price_with_lag;

-- 3. Volatility (Standard Deviation over time)
SELECT 
    asset_id,
    STDDEV(price) OVER (PARTITION BY asset_id ORDER BY time ROWS BETWEEN 20 PRECEDING AND CURRENT ROW) AS volatility
FROM market_data;

-- 4. Top Profitable Users (Based on realized and unrealized P/L)
SELECT 
    u.username,
    COALESCE(SUM(ps.unrealized_pl), 0) AS total_pl
FROM users u
LEFT JOIN portfolio_summary ps ON u.user_id = ps.user_id
GROUP BY u.user_id, u.username
ORDER BY total_pl DESC
LIMIT 10;

-- 5. Most Traded Assets (By Trade Count)
SELECT
    a.symbol,
    COUNT(t.trade_id) AS trade_count,
    SUM(t.quantity * t.price) AS volume
FROM trades t
JOIN assets a ON t.asset_id = a.asset_id
GROUP BY a.symbol
ORDER BY trade_count DESC;

-- 6. User Trading Activity (Daily Aggregation using TimescaleDB time_bucket)
SELECT
    user_id,
    time_bucket('1 day', executed_at) AS day,
    COUNT(*) AS total_trades,
    SUM(CASE WHEN trade_type = 'buy' THEN quantity * price ELSE 0 END) AS buy_volume,
    SUM(CASE WHEN trade_type = 'sell' THEN quantity * price ELSE 0 END) AS sell_volume
FROM trades
GROUP BY user_id, day
ORDER BY day DESC;