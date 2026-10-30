-- Spring Data's list index must not overwrite the business product_id.
ALTER TABLE order_items ADD COLUMN item_position INT;
UPDATE order_items
SET item_position = CASE WHEN product_id REGEXP '^[0-9]+$' THEN CAST(product_id AS UNSIGNED) ELSE 0 END;
