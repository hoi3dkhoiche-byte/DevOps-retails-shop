ALTER TABLE order_items ADD COLUMN item_position INT;
UPDATE order_items
SET item_position = CASE WHEN REGEXP_LIKE(product_id, '^[0-9]+$') THEN CAST(product_id AS INTEGER) ELSE 0 END;
