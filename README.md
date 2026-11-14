# docker-iot
Docker compose environment for IOT on Raspberry Pi

## Manual installation
 * install Raspbian
 * update raspbian apt-get update && apt-get upgrade
 * enable ssh with sudo raspi-config
 * upload ssh key to ~/.ssh/authorized_keys
 * create ssh key with ssh-keygen and upload it to github

## Ansible
 * run ./run.sh

## Containers

| Container | Purpose |
|---|---|
| nginx | HTTPS proxy for Grafana |
| influxdb | InfluxDB 2 (org `jg`, bucket `sensordata`) |
| telegraf | MQTT → InfluxDB |
| grafana | Dashboards, data sources InfluxDB and MariaDB |
| mqtt | Mosquitto |
| uptime-kuma | Uptime monitoring |
| mariadb | MariaDB 11.4, database `altherma4` (only on `127.0.0.1:3306`) |
| altherma4 | Daikin Altherma 4: reads Modbus TCP → MariaDB + InfluxDB, alarms via Telegram/e-mail, weekly report |

Setting up the Altherma containers (new `.env` variables, Grafana queries):
see [altherma4/README.md](altherma4/README.md).
