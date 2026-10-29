CREATE TABLE order_outbox (
    event_id CHAR(36) PRIMARY KEY,
    payload LONGTEXT NOT NULL,
    created_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6),
    sent_at TIMESTAMP(6) NULL,
    INDEX pending_events (sent_at, created_at)
) ENGINE=InnoDB;

CREATE TABLE consumed_order_events (
    event_id CHAR(36) PRIMARY KEY,
    order_id VARCHAR(255) NOT NULL,
    processed_at TIMESTAMP(6) NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
) ENGINE=InnoDB;
