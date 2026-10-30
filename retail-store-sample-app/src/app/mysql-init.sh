#!/bin/sh
# MySQL sources non-executable init files on Linux. Keep shell options local so
# nounset cannot break the parent entrypoint after this script has returned.
(
set -eu
# Disposable local/CI fixture only. AWS credentials are bootstrapped out of band.
for service in catalog cart orders; do
  MYSQL_PWD="$MYSQL_ROOT_PASSWORD" mysql -uroot <<SQL
CREATE DATABASE IF NOT EXISTS ${service}_ci;
CREATE USER IF NOT EXISTS '${service}_user'@'%' IDENTIFIED BY 'ci-only-password';
GRANT ALL PRIVILEGES ON ${service}_ci.* TO '${service}_user'@'%';
SQL
done
)
