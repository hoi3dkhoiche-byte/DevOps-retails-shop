-- Disposable local/CI fixture only. AWS credentials are bootstrapped out of band.
-- Let the image entrypoint choose the initialization socket and root credentials.
CREATE DATABASE IF NOT EXISTS catalog_ci;
CREATE USER IF NOT EXISTS 'catalog_user'@'%' IDENTIFIED BY 'ci-only-password';
GRANT ALL PRIVILEGES ON catalog_ci.* TO 'catalog_user'@'%';

CREATE DATABASE IF NOT EXISTS cart_ci;
CREATE USER IF NOT EXISTS 'cart_user'@'%' IDENTIFIED BY 'ci-only-password';
GRANT ALL PRIVILEGES ON cart_ci.* TO 'cart_user'@'%';

CREATE DATABASE IF NOT EXISTS orders_ci;
CREATE USER IF NOT EXISTS 'orders_user'@'%' IDENTIFIED BY 'ci-only-password';
GRANT ALL PRIVILEGES ON orders_ci.* TO 'orders_user'@'%';
