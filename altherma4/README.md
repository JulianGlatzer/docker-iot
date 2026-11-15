# Daikin Altherma 4 – Modbus logger (read-only)

Reads the Altherma 4 every 15 s via Modbus TCP and writes every poll to
**MariaDB** (container `mariadb`) and **InfluxDB 2** (existing container
`influxdb`, org `jg`, bucket `sensordata`, measurement `altherma4`).
The logger only sends read requests (Modbus function codes 0x01–0x04);
anything else is rejected in the code before it reaches the network.

## Setup

1. Copy the variables from `env.example` into the `.env` in the repository root
   and set the passwords (e.g. `openssl rand -hex 24`). `ALTHERMA_HOST` is required.
2. InfluxDB: the logger uses the existing `INFLUXDB_ADMIN_TOKEN` (like telegraf
   and grafana). A dedicated token with write access to `sensordata` only would be safer:
   ```bash
   BUCKET_ID=$(docker exec influxdb influx bucket list --org jg --name sensordata --hide-headers | cut -f1)
   docker exec influxdb influx auth create --org jg --write-bucket "$BUCKET_ID" --description altherma4-logger
   ```
   and then point `INFLUXDB_TOKEN` of the `altherma4` service in `docker-compose.yml` to it.
3. Start:
   ```bash
   docker-compose up -d mariadb
   docker-compose up -d --build altherma4 grafana
   docker-compose logs -f altherma4
   ```

Notifications use the existing Grafana variables in the `.env`:
`GF_TELEGRAM_BOT_TOKEN`, `GF_TELEGRAM_CHAT_ID`, `SMTP_HOST` (also in the format
`host:port`), `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM_ADDRESS`, `GF_NOTIFICATION_EMAIL`.

**Important:** MariaDB users and passwords are only created on the *first*
start with an empty `/data/mariadb`. Later changes in `.env` have no effect
there; change them in MariaDB itself.

## Access

| What | How |
|---|---|
| Grafana | data sources `InfluxDB` (as before) and `MariaDB Altherma` (read-only, user `grafana`) |
| MariaDB on the Pi | `127.0.0.1:3306`, user `altherma4` |
| MariaDB console | `docker exec -it mariadb mariadb -u altherma4 -p altherma4` |
| Single report | `docker-compose run --rm altherma4 --once` |
| Register scan | `docker-compose run --rm altherma4 --scan 300` |

In Grafana, filter MariaDB queries on `ts_unix`, because `ts` holds local time
without a time zone:
```sql
SELECT FROM_UNIXTIME(ts_unix) AS time, calc_q_hp_kw, p_el
FROM samples WHERE ts_unix BETWEEN $__unixEpochFrom() AND $__unixEpochTo() ORDER BY ts_unix;
```

## Data model

* **MariaDB:** table `samples` (one row per poll), `columns_meta` (description
  of every column), `logger_state` (date of the last weekly report), views
  `v_day`, `v_compressor_starts`, `v_defrosts`, `v_backup_heater`, `v_settings`.
  Prefixes: none = register value, `calc_` = calculated, `diag_` = poll
  diagnostics, `ext_` = external sensors, `raw_json` = all raw values.
* **InfluxDB:** measurement `altherma4`, tag `device=altherma4`, same field
  names. All numbers as float, bools 0/1, no `raw_json`.

Example (MariaDB):
```sql
SELECT * FROM v_day ORDER BY day DESC LIMIT 7;
```

Example (Flux, InfluxDB Data Explorer):
```flux
from(bucket: "sensordata")
  |> range(start: -24h)
  |> filter(fn: (r) => r._measurement == "altherma4")
  |> filter(fn: (r) => r._field == "calc_q_hp_kw" or r._field == "p_el" or r._field == "t_outdoor")
  |> aggregateWindow(every: 5m, fn: mean)
```

## Failure behaviour

* If MariaDB or InfluxDB is unreachable, the logger buffers up to
  `BUFFER_ROWS` rows (default 20,000 ≈ 3.5 days at 15 s) in memory and writes
  them later. A buffer not yet written is lost when the logger container restarts.
* If the heat pump is unreachable, a row with `ok = 0` and the error type is
  stored; after ~3 min the container is marked `unhealthy`.

## Notifications (Telegram, e-mail)

Every alarm is reported when it triggers and when it clears, each time with
operating data in the text. All thresholds are in `.env`; `0` disables an alarm.

| Event | Condition (default) |
|---|---|
| 🔴 Fault / warning | IR21 = 1 or 2, with the code as on the display (e.g. `7H-19`); a new code = a new message |
| 🔴 Emergency operation | DI24 active |
| 🔴 No connection | heat pump unreachable for more than 5 min |
| 🔴 Flow too low | < 3 l/min with the pump running, for more than 2 min |
| ⚠️ Water pressure low | < `ALERT_PRESSURE_MIN` |
| ⚠️ Pressure drop | pressure falls by ≥ 0.2 bar within 24 h, compared only at the same pump speed (± 5 %), because the reading includes the pump head (~0.6 bar between 30 % and 85 %) |
| ⚠️ Backup heater running | relays on for more than 10 min (defrost excluded) |
| ⚠️ Backup heater daily energy | once per day, as soon as ≥ 2 kWh |
| ⚠️ Compressor short cycling | more than 3 starts in 60 min |
| ⚠️ Leaving water below setpoint | > 5 K below target, for more than 30 min (compressor on, no DHW/defrost) |
| ⚠️ Defrost | longer than 15 min or more than 2 per hour |
| ⚠️ Disinfection failed | IR63 = 0 |
| ⚠️ Frost protection with cold water | off by default (`ALERT_FROST=1`): DI14/DI15 active **and** leaving or return water < 10 °C. Frost protection alone is normal below ~6 °C outdoor |
| ⚠️ Database not writing | MariaDB or InfluxDB for more than 30 min, or buffer ≥ 80 % |
| ⚠️ Separator: heat pump circulates more | supplies equal, heat pump return > secondary return + 1 K, for more than 60 min (secondary sensors only) |
| ⚠️ Separator: heating circuits circulate more | secondary supply < heat pump leaving water − 1 K, returns equal, for more than 60 min (secondary sensors only) |

**Dead man's switch:** if the logger or the Pi itself fails, it cannot report
anything. For this it calls `ALTHERMA_HEARTBEAT_URL` every 5 min (e.g.
[healthchecks.io](https://healthchecks.io), free); if the call stops, the
external service notifies you. Strongly recommended.

**Weekly report:** every Monday from 7:00 (configurable) over all channels –
electricity, heat, SPF (with previous week), backup heater, compressor hours
and starts, defrosts, outdoor temperature, pressure trend, faults, data
coverage, plus one line per day. Calculated from MariaDB; the send date is
stored in `logger_state` so that a restart does not send it twice.
Generate immediately: `docker-compose run --rm altherma4 --report-now`.

An alarm triggers/clears only after `ALERT_CONFIRM` polls in a row (against
false alarms). If a condition persists, a reminder is sent every
`ALERT_REPEAT_HOURS`. Sending runs in the background with up to 4 attempts
(immediately, 30 s, 2 min, 10 min).

Test: `docker-compose run --rm altherma4 --test-notify`

Note: the alarm state is not persisted – if a fault is still present when the
container restarts, it is reported again.

## Hydraulic separator

The Altherma only measures its own side of the separator (leaving water IR41,
return IR42, flow IR49). With two sensors on the **secondary** supply and return
(`SEC_SUPPLY_SOURCE`, `SEC_RETURN_SOURCE`) the logger evaluates how both sides match:

| Observation | Meaning | Action |
|---|---|---|
| secondary supply ≈ heat pump leaving water, heat pump return clearly warmer than secondary return | the heat pump circulates more than the heating circuits take; hot water flows straight back | increase the ΔT setpoint ([1.14]/[2.14]) or lower the minimum flow |
| secondary supply colder than heat pump leaving water, heat pump return ≈ secondary return | the heating circuits circulate more; return water mixes into the supply | first lower the pumps [3.13.x], then reduce the ΔT setpoint if needed |
| both supplies ≈ equal, both returns ≈ equal | flows match | leave as is |

Evaluated only with the compressor running, without defrost/DHW, after 10 min
of settling and averaged over 5 min (`HYD_*`). Columns: `ext_t_sec_supply`,
`ext_t_sec_return` (sensors) and `calc_sep_*` (ΔLWT, ΔRET, estimated secondary
flow = heat pump flow × heat pump ΔT / secondary ΔT, verdict).
The weekly report shows the share of time per state.

Sensors:
* **DS18B20 (1-Wire)** on the Pi: `dtoverlay=w1-gpio` in `/boot/firmware/config.txt`,
  sensor on the pipe with thermal paste and insulation. `/sys/bus/w1/devices`
  is visible in the container; get the ID with `ls /sys/bus/w1/devices` → `SEC_SUPPLY_SOURCE=w1:28-…`.
* **Modbus TCP** (e.g. an ESP32): `modbus://ip:502/unit/ir/address[/scale]`,
  read function codes only.
* With a balanced separator, calibrate a fixed offset with `SEC_*_OFFSET` –
  strap-on sensors often read 0.5–1 K too low.

Without extra sensors there is only a hint: `calc_sep_hint_dlwt_k` = IR41 − IR72
with the Bizone mixing valve fully open. This is only meaningful if the mixing
kit sits downstream of the separator.

## Options

See `env.example` and `docker-compose run --rm altherma4 --help`.
