CREATE TABLE orders (
    id VARCHAR(255) PRIMARY KEY,
    created_date TIMESTAMP(6)
) ENGINE=InnoDB;

CREATE TABLE order_items (
    product_id VARCHAR(255) NOT NULL,
    quantity INT,
    unit_cost INT,
    total_cost INT,
    order_id VARCHAR(255),
    FOREIGN KEY (order_id) REFERENCES orders(id)
) ENGINE=InnoDB;

CREATE TABLE shipping_addresses (
    order_id VARCHAR(255) PRIMARY KEY,
    first_name VARCHAR(255) NOT NULL,
    last_name VARCHAR(255) NOT NULL,
    email VARCHAR(255) NOT NULL,
    address1 VARCHAR(255) NOT NULL,
    address2 VARCHAR(255),
    city VARCHAR(255) NOT NULL,
    zip_code VARCHAR(255) NOT NULL,
    state_id VARCHAR(255) NOT NULL,
    FOREIGN KEY (order_id) REFERENCES orders(id)
) ENGINE=InnoDB;
