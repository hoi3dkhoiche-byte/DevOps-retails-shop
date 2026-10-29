CREATE TABLE carts (
    customer_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin PRIMARY KEY
) ENGINE=InnoDB;

CREATE TABLE cart_items (
    customer_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    item_id VARCHAR(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NOT NULL,
    quantity INT NOT NULL,
    unit_price INT NOT NULL,
    PRIMARY KEY (customer_id, item_id),
    FOREIGN KEY (customer_id) REFERENCES carts(customer_id) ON DELETE CASCADE
) ENGINE=InnoDB;
