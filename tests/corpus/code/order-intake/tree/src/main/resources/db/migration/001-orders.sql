CREATE TABLE orders (
    id BIGINT PRIMARY KEY,
    external_id VARCHAR(64) NOT NULL,
    supplier VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL
);

CREATE UNIQUE INDEX orders_external_id_uq ON orders (external_id);
