-- 0002_orders_expiry_index
--
-- expire_stale_orders() runs every 5 minutes and sweeps:
--
--     UPDATE orders SET status = 'cancelled'
--     WHERE status = 'open' AND expires_at IS NOT NULL AND expires_at <= NOW()
--
-- Before this migration the planner reached for idx_orders_open_asset_kind_target
-- (asset_id, order_kind, status, target_price). With only a `status` predicate
-- the leading columns are skipped, so that becomes a full index scan and
-- expires_at is filtered in the heap afterwards.
--
-- A partial index containing only open orders that can actually expire answers
-- the predicate directly and stays small: orders without an expiry, and every
-- filled or cancelled order, are excluded from it entirely.

CREATE INDEX IF NOT EXISTS idx_orders_pending_expiry
    ON orders (expires_at)
    WHERE status = 'open' AND expires_at IS NOT NULL;

COMMENT ON INDEX idx_orders_pending_expiry IS
    'Supports the expire_stale_orders() sweep. Partial: open, expiring orders only.';
