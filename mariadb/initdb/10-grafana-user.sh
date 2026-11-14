#!/bin/sh
# Runs only on the first start with an empty /data/mariadb (official MariaDB image).
# Creates the read-only user for the Grafana data source.
set -eu
if [ -z "${MARIADB_GRAFANA_PASSWORD:-}" ]; then
  echo "MARIADB_GRAFANA_PASSWORD empty – user grafana is not created"
  exit 0
fi
mariadb --protocol=socket -uroot -p"${MARIADB_ROOT_PASSWORD}" <<SQL
CREATE USER IF NOT EXISTS 'grafana'@'%' IDENTIFIED BY '${MARIADB_GRAFANA_PASSWORD}';
GRANT SELECT, SHOW VIEW ON \`${MARIADB_DATABASE}\`.* TO 'grafana'@'%';
FLUSH PRIVILEGES;
SQL
