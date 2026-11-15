#!/usr/bin/env python3
"""
Daikin Altherma 4 – Modbus TCP logger (STRICTLY READ-ONLY)

Reads all known registers of the Altherma 4 in an endless loop (default every
15 s; settings every 300 s) and writes every poll to MySQL/MariaDB and/or
InfluxDB 2 (each optional, at least one required).
Configuration via arguments or environment variables (Docker: .env), see --help.
If a database is down, the logger buffers in memory and writes later.

Origin of the values
--------------------
* Register values:        columns without prefix (e.g. t_lwt_buh, d_buh_r1)
* CALCULATED values:      prefix "calc_"  (console: [CALCULATED])
* Poll diagnostics:       prefix "diag_"
* External sensors:       prefix "ext_"   (e.g. ext_t_sec_supply, secondary side of the separator)
* Raw values of all regs: column "raw_json" (for later recalculation)

READ-ONLY GUARANTEE
-------------------
* No Modbus library – own minimal client on socket level.
* Only the four READ function codes are allowed
      0x01 Read Coils             0x02 Read Discrete Inputs
      0x03 Read Holding Registers 0x04 Read Input Registers
  Any other function code (in particular 0x05/0x06/0x0F/0x10/0x16/0x17 = write)
  is rejected with PermissionError BEFORE sending. There is no function in
  the code that builds a write request.

REGISTER SOURCE
---------------
Daikin "Guide de référence pour la configuration – MMI", 4P773396-1D
(2026.08), chapter 7.2. Verified on the unit (MMI 4.2.0) against the display:
IR40/41/42/44/49/72/74/76/77/79, IR69/70 (= PWM signal, 0 % = full speed).
Addresses are 1-based (documentation), PDU address = address - 1.
Temp16/Pow16: signed /100.
Special values (per joklee/gerione): 32765 = not available/error state,
32766 = no error/not supported, 32767 = register not supported
-> all three are stored as NULL (raw value stays in raw_json).
Max. 3 simultaneous Modbus connections (ports 502 + 802 combined).

LIMITATIONS
-----------
* Electrical power only via IR51 (Pow16, in practice 0.1 kW steps). No
  per-phase values, no kWh counters via Modbus.
* Whether IR51 includes the backup heater/pumps is not documented.
* Backup heater: only relay states (DI2-7). calc_q_buh_kw (ΔT method) is the
  best approximation; calc_q_buh_clean_kw sets it to 0 when all relays are off.
* Coils 4-7 and DI26 are not in the v4 documentation.
* Settings (HR/CO) are read only every --slow-interval s; in the rows in
  between these columns are NULL (diag_slow = 0).

MYSQL / MARIADB (schema is created/extended automatically)
-----------------------------------------------------------
Tables:   samples (one row per poll), columns_meta (column descriptions),
          logger_state (e.g. date of the last weekly report).
Views:    v_day                – daily values: kWh, SPF, runtime,
                                 compressor starts, defrosts, backup heater, pressure
          v_compressor_starts  – every compressor start with run time
          v_defrosts           – every defrost with duration
          v_backup_heater      – every backup heater run with duration and kWh
          v_settings           – only rows with settings read
Example:  SELECT * FROM v_day ORDER BY day DESC LIMIT 7;

INFLUXDB 2
----------
Measurement "altherma4", tag device=altherma4, one field per column (same
names as in SQL, incl. calc_*/diag_*). All numbers as float, bools as 0/1,
text as string fields; raw_json only in SQL. Timestamps in seconds.

Usage:
    docker compose up -d                                      # see README.md
    python3 altherma4_read.py 192.168.x.y --once              # single poll, full report
    python3 altherma4_read.py 192.168.x.y --scan 300          # raw scan of addresses 1..300
"""

from __future__ import annotations

import argparse
import email.utils
import json
import os
import queue
import signal
import smtplib
import socket
import ssl
import struct
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime
from email.message import EmailMessage

# ===========================================================================
# 1) Minimal, strictly read-only Modbus TCP client
# ===========================================================================

FC_READ_COILS = 0x01
FC_READ_DISCRETE = 0x02
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
ALLOWED_FUNCTION_CODES = frozenset({FC_READ_COILS, FC_READ_DISCRETE, FC_READ_HOLDING, FC_READ_INPUT})

NOT_AVAILABLE = 32766
FLOW_SCALE = 0.01  # adjustable via --flow-scale (see docstring)


class ModbusError(Exception):
    pass


class ReadOnlyModbusTCP:
    """Speaks only FC 0x01-0x04 (read). There are no write functions."""

    def __init__(self, host: str, port: int = 502, unit_id: int = 1, timeout: float = 5.0):
        self.host, self.port, self.unit_id, self.timeout = host, port, unit_id, timeout
        self._sock: socket.socket | None = None
        self._tid = 0
        self.n_requests = 0      # diagnostics: requests sent
        self.n_exceptions = 0    # diagnostics: Modbus exceptions (e.g. gaps)

    def __enter__(self):
        self._sock = socket.create_connection((self.host, self.port), timeout=self.timeout)
        return self

    def __exit__(self, *exc):
        if self._sock:
            self._sock.close()
            self._sock = None

    def _recv_exact(self, n: int) -> bytes:
        buf = b""
        while len(buf) < n:
            chunk = self._sock.recv(n - len(buf))
            if not chunk:
                raise ModbusError("connection closed by the device")
            buf += chunk
        return buf

    def _request(self, fc: int, doc_address: int, count: int) -> bytes:
        # ---- central read-only lock ----
        if fc not in ALLOWED_FUNCTION_CODES:
            raise PermissionError(f"function code 0x{fc:02X} is blocked (read-only)")
        max_count = 2000 if fc in (FC_READ_COILS, FC_READ_DISCRETE) else 125
        if not (1 <= doc_address <= 65535 and 1 <= count <= max_count):
            raise ValueError("invalid address/count")

        self._tid = (self._tid + 1) & 0xFFFF
        self.n_requests += 1
        pdu = struct.pack(">BHH", fc, doc_address - 1, count)  # 1-based -> 0-based
        self._sock.sendall(struct.pack(">HHHB", self._tid, 0, len(pdu) + 1, self.unit_id) + pdu)

        tid, _proto, length, _unit = struct.unpack(">HHHB", self._recv_exact(7))
        body = self._recv_exact(length - 1)
        if tid != self._tid:
            raise ModbusError(f"transaction ID mismatch ({tid} != {self._tid})")
        if body[0] == fc | 0x80:
            self.n_exceptions += 1
            raise ModbusError(f"Modbus exception {body[1]} (FC 0x{fc:02X}, addr. {doc_address})")
        if body[0] != fc:
            raise ModbusError(f"unexpected function code 0x{body[0]:02X}")
        return body[2:2 + body[1]]

    def read_registers(self, fc: int, doc_address: int, count: int) -> list[int]:
        if fc not in (FC_READ_HOLDING, FC_READ_INPUT):
            raise ValueError("only FC 0x03/0x04")
        data = self._request(fc, doc_address, count)
        return list(struct.unpack(f">{count}H", data[: 2 * count]))

    def read_bits(self, fc: int, doc_address: int, count: int) -> list[bool]:
        if fc not in (FC_READ_COILS, FC_READ_DISCRETE):
            raise ValueError("only FC 0x01/0x02")
        data = self._request(fc, doc_address, count)
        return [bool(data[i // 8] >> (i % 8) & 1) for i in range(count)]


# ===========================================================================
# 2) Register definitions
#    Types: t16 = signed x0.01 (temperature/power/pressure/flow)
#           i16 = signed integer   txt = 2 ASCII characters   bit = bool
# ===========================================================================

E_ONOFF = {0: "off", 1: "on"}
E_HEATCOOL = {0: "none", 1: "heating", 2: "cooling"}
E_IDLE = {0: "idle/buffer", 1: "running"}
E_WEATHER = {0: "fixed", 1: "weather-dependent"}

# (address, key, description, type, unit, enum)
INPUT_REGS = [
    (21, "fault",               "Fault",                                    "i16", "", {0: "none", 1: "error", 2: "warning"}),
    (22, "error_code",          "Error code",                               "txt", "", None),
    (23, "error_subcode",       "Error sub-code",                           "i16", "", None),
    (30, "circ_pump",           "Circulation pump running",                 "i16", "", E_ONOFF),
    (31, "compressor",          "Compressor running",                       "i16", "", E_ONOFF),
    (32, "booster_run",         "Booster heater running",                   "i16", "", E_ONOFF),
    (33, "disinfection",        "Disinfection running",                     "i16", "", E_ONOFF),
    (35, "defrost",             "Defrost/restart",                          "i16", "", E_ONOFF),
    (36, "hot_start",           "Hot start",                                "i16", "", E_ONOFF),
    (37, "three_way_valve",     "3-way valve",                              "i16", "", {0: "space heating", 1: "DHW"}),
    (38, "operation_mode",      "Operation mode",                           "i16", "", E_HEATCOOL),
    (40, "t_lwt_phe",           "Leaving water after plate heat exchanger", "t16", "°C", None),
    (41, "t_lwt_buh",           "Leaving water after backup heater (BUH)",  "t16", "°C", None),
    (42, "t_return",            "Return water",                             "t16", "°C", None),
    (43, "t_dhw",               "Domestic hot water",                       "t16", "°C", None),
    (44, "t_outdoor",           "Outdoor temperature",                      "t16", "°C", None),
    (45, "t_refrigerant",       "Refrigerant liquid",                       "t16", "°C", None),
    (49, "flow",                "Flow rate (scaling see --flow-scale)",     "flow", "l/min", None),
    (50, "t_room_main",         "Room temperature main zone (user interface)", "t16", "°C", None),
    (51, "p_el",                "Electrical power consumption",             "t16", "kW", None),
    (52, "dhw_normal",          "DHW normal operation",                     "i16", "", E_IDLE),
    (53, "sh_normal",           "Space heating/cooling normal operation",   "i16", "", E_IDLE),
    (54, "main_heat_lwt_min",   "Main zone heating LWT setpoint min",       "t16", "°C", None),
    (55, "main_heat_lwt_max",   "Main zone heating LWT setpoint max",       "t16", "°C", None),
    (56, "main_cool_lwt_min",   "Main zone cooling LWT setpoint min",       "t16", "°C", None),
    (57, "main_cool_lwt_max",   "Main zone cooling LWT setpoint max",       "t16", "°C", None),
    (58, "add_heat_lwt_min",    "Additional zone heating LWT setpoint min", "t16", "°C", None),
    (59, "add_heat_lwt_max",    "Additional zone heating LWT setpoint max", "t16", "°C", None),
    (60, "add_cool_lwt_min",    "Additional zone cooling LWT setpoint min", "t16", "°C", None),
    (61, "add_cool_lwt_max",    "Additional zone cooling LWT setpoint max", "t16", "°C", None),
    (63, "disinfection_status", "Disinfection status",                      "i16", "", {0: "failed", 1: "successful", 2: "holding", 3: "heating up"}),
    (64, "holiday",             "Holiday mode",                             "i16", "", E_ONOFF),
    (65, "sg_status",           "Demand response / SG status",              "i16", "", {0: "free", 1: "forced off", 2: "forced on", 3: "recommended on", 4: "reduced"}),
    (66, "bypass_valve",        "Bypass valve position",                    "i16", "%", None),
    (67, "tank_valve",          "Tank valve position",                      "i16", "%", None),
    (68, "pump_speed",          "Circulation pump speed",                   "i16", "%", None),
    (69, "mix_pump_pwm",        "Bizone: mixed pump PWM signal (0% = full speed)",  "i16", "%", None),
    (70, "direct_pump_pwm",     "Bizone: direct pump PWM signal (0% = full speed)", "i16", "%", None),
    (71, "mixing_valve",        "Bizone: mixing valve position",            "i16", "%", None),
    (72, "t_lwt_mixed",         "Bizone: mixed leaving water",              "t16", "°C", None),
    (73, "mix_target_main",     "Bizone: main zone setpoint",               "t16", "°C", None),
    (74, "t_lwt_pre_phe",       "Leaving water before PHE (outdoor unit)",  "t16", "°C", None),
    (75, "t_lwt_tank_valve",    "Leaving water at tank valve",              "t16", "°C", None),
    (76, "t_dhw_top",           "DHW top",                                  "t16", "°C", None),
    (77, "t_dhw_bottom",        "DHW bottom",                               "t16", "°C", None),
    (78, "t_room_add",          "Room temperature additional zone (user interface)", "t16", "°C", None),
    (79, "water_pressure",      "Water pressure",                           "t16", "bar", None),
    (80, "lwt_target_main",     "Target leaving water main zone",           "t16", "°C", None),
    (81, "lwt_target_add",      "Target leaving water additional zone",     "t16", "°C", None),
    (82, "fault_counter",       "Fault counter",                            "i16", "", None),
    (83, "unit_mode",           "Unit operation",                           "i16", "", {0: "stop", 1: "tank charging", 2: "heating", 3: "cooling", 4: "actuator"}),
    (84, "room_heat_min",       "Room heating setpoint min",                "t16", "°C", None),
    (85, "room_heat_max",       "Room heating setpoint max",                "t16", "°C", None),
    (86, "room_cool_min",       "Room cooling setpoint min",                "t16", "°C", None),
    (87, "room_cool_max",       "Room cooling setpoint max",                "t16", "°C", None),
    (138, "restart_in",         "Time until model restart (v4)",            "i16", "min", None),
    (137, "undoc_ir137",        "UNDOCUMENTED IR137 (scan: 4)",             "i16", "", None),
    (139, "operating_state",    "Operating state (v4)",                     "i16", "", {0: "normal", 1: "maintenance", 2: "restart pending"}),
    (140, "undoc_ir140",        "UNDOCUMENTED IR140 (scan: 2)",             "i16", "", None),
]

HOLDING_REGS = [
    (1,  "s_lwt_heat_main",      "LWT setpoint heating main zone",           "i16", "°C", None),
    (2,  "s_lwt_cool_main",      "LWT setpoint cooling main zone",           "i16", "°C", None),
    (3,  "s_mode",               "Operation mode (n/a on heating-only units)", "i16", "", {0: "auto", 1: "heating", 2: "cooling"}),
    (4,  "s_space_heating",      "Space heating/cooling ON/OFF",             "i16", "", E_ONOFF),
    (6,  "s_room_heat_main_int", "Room setpoint heating main zone (integer)", "i16", "°C", None),
    (7,  "s_room_cool_main_int", "Room setpoint cooling main zone (integer)", "i16", "°C", None),
    (9,  "s_quiet",              "Quiet mode",                               "i16", "", {0: "off", 1: "auto", 2: "manual"}),
    (10, "s_dhw_reheat",         "DHW reheat setpoint",                      "i16", "°C", None),
    (13, "s_dhw_powerful",       "DHW powerful (booster) ON/OFF",            "i16", "", E_ONOFF),
    (14, "s_dhw_powerful_target", "DHW powerful setpoint",                   "t16", "°C", None),
    (15, "s_dhw_single",         "DHW single heat-up ON/OFF",                "i16", "", E_ONOFF),
    (16, "s_dhw_single_target",  "DHW single heat-up setpoint",              "t16", "°C", None),
    (54, "s_offset_heat_main",   "Heating curve offset heating main zone",   "i16", "K", None),
    (55, "s_offset_cool_main",   "Heating curve offset cooling main zone",   "i16", "K", None),
    (56, "s_smartgrid",          "Smart grid operation mode",                "i16", "", {0: "free", 1: "forced off", 2: "recommended on", 3: "forced on"}),
    (58, "s_power_limit",        "Requested power limit",                    "t16", "kW", None),
    (63, "s_lwt_heat_add",       "LWT setpoint heating additional zone",     "i16", "°C", None),
    (64, "s_lwt_cool_add",       "LWT setpoint cooling additional zone",     "i16", "°C", None),
    (66, "s_offset_heat_add",    "Heating curve offset heating additional zone", "i16", "K", None),
    (67, "s_offset_cool_add",    "Heating curve offset cooling additional zone", "i16", "K", None),
    (68, "s_weather_heat_main",  "Heating main zone control type",           "i16", "", E_WEATHER),
    (69, "s_weather_cool_main",  "Cooling main zone control type",           "i16", "", E_WEATHER),
    (74, "s_demand_main",        "Thermostat demand main zone",              "i16", "", E_HEATCOOL),
    (75, "s_demand_add",         "Thermostat demand additional zone",        "i16", "", E_HEATCOOL),
    (76, "s_room_heat_main",     "Room setpoint heating main zone",          "t16", "°C", None),
    (77, "s_room_cool_main",     "Room setpoint cooling main zone",          "t16", "°C", None),
    (78, "s_room_heat_add",      "Room setpoint heating additional zone",    "t16", "°C", None),
    (79, "s_room_cool_add",      "Room setpoint cooling additional zone",    "t16", "°C", None),
    (80, "s_dhw_mode",           "DHW mode",                                 "i16", "", {0: "reheat", 1: "schedule+reheat", 2: "schedule", NOT_AVAILABLE: "off"}),
    (81, "s_restart",            "Execute model restart (v4)",               "i16", "", {0: "no", 1: "restart"}),
]

# (address, key, description)
DISCRETE_INPUTS = [
    (1,  "d_shutoff_valve",    "Shut-off valve"),
    (2,  "d_buh_r1",           "Backup heater relay 1"),
    (3,  "d_buh_r2",           "Backup heater relay 2"),
    (4,  "d_buh_r3",           "Backup heater relay 3"),
    (5,  "d_buh_r4",           "Backup heater relay 4"),
    (6,  "d_buh_r5",           "Backup heater relay 5"),
    (7,  "d_buh_r6",           "Backup heater relay 6"),
    (8,  "d_booster",          "Booster heater (tank)"),
    (9,  "d_tank_boiler",      "Tank boiler"),
    (10, "d_bivalent",         "Bivalent"),
    (11, "d_compressor",       "Compressor running"),
    (12, "d_quiet",            "Quiet mode active"),
    (13, "d_holiday",          "Holiday mode active"),
    (14, "d_frost_protection", "Frost protection (room)"),
    (15, "d_pipe_freeze",      "Pipe freeze protection"),
    (16, "d_disinfection",     "Disinfection"),
    (17, "d_defrost",          "Defrost"),
    (18, "d_hot_start",        "Hot start"),
    (19, "d_dhw",              "DHW operation"),
    (20, "d_main_zone",        "Main zone running"),
    (21, "d_add_zone",         "Additional zone running"),
    (22, "d_powerful_req",     "Powerful heat-up requested"),
    (23, "d_manual_req",       "Manual heat-up requested"),
    (24, "d_emergency",        "Emergency operation active"),
    (25, "d_circ_pump",        "Circulation pump running"),
    (26, "d_limit_accepted",   "Power limit accepted (not in v4 documentation)"),
    (27, "d_undoc_27",         "UNDOCUMENTED DI27"),
    (28, "d_undoc_28",         "UNDOCUMENTED DI28"),
    (29, "d_undoc_29",         "UNDOCUMENTED DI29"),
    (30, "d_undoc_30",         "UNDOCUMENTED DI30"),
]

COILS = [
    (1, "c_dhw",             "DHW ON/OFF"),
    (2, "c_main_zone",       "Main zone ON/OFF"),
    (3, "c_add_zone",        "Additional zone ON/OFF"),
    # 4-7: NOT in Daikin documentation 4P773396-1D; present according to gerione/daikin-ha-ekrhh-modbus
    (4, "c_sched_heat_main", "Heating schedule main zone active (undoc.)"),
    (5, "c_sched_cool_main", "Cooling schedule main zone active (undoc.)"),
    (6, "c_sched_heat_add",  "Heating schedule additional zone active (undoc.)"),
    (7, "c_sched_cool_add",  "Cooling schedule additional zone active (undoc.)"),
]




# Read blocks (contiguous; on exception, single access)
# Groups of sub-blocks per Daikin documentation (gaps = undocumented addresses).
# For each group the whole range is tried in ONE request first; if that fails
# (device rejects gaps), this is remembered and the sub-blocks are read from
# then on, register by register as a last resort.
INPUT_BLOCKS = [[(21, 23)],
                [(30, 33), (35, 38), (40, 45), (49, 61), (63, 87)],
                [(137, 140)]]
HOLDING_BLOCKS = [[(1, 4), (6, 7), (9, 10), (13, 16)],
                  [(54, 56), (58, 58), (63, 64), (66, 69), (74, 81)]]
_FAILED_SPANS: set[tuple[int, int, int]] = set()

# Special values per Daikin documentation/joklee/gerione -> NULL
SPECIAL_VALUES = {32765: "not available/error state",
                  32766: "no error/not supported",
                  32767: "register not supported"}


# ===========================================================================
# 3) Reading & decoding
# ===========================================================================

def s16(v: int) -> int:
    return v - 0x10000 if v & 0x8000 else v


def decode(raw: int | None, typ: str):
    if raw is None:
        return None
    if raw in SPECIAL_VALUES:
        return None
    if typ == "txt":
        txt = bytes([(raw >> 8) & 0xFF, raw & 0xFF]).decode("ascii", "replace").strip("\x00 ")
        return txt or None
    sv = s16(raw)
    if typ == "flow":
        return round(sv * FLOW_SCALE, 2)
    return round(sv * 0.01, 2) if typ == "t16" else sv


def read_reg_blocks(cli, fc, groups) -> dict[int, int | None]:
    out: dict[int, int | None] = {}
    for group in groups:
        start, end = group[0][0], group[-1][1]
        if len(group) > 1 and (fc, start, end) not in _FAILED_SPANS:
            try:
                vals = cli.read_registers(fc, start, end - start + 1)
                out.update({start + i: v for i, v in enumerate(vals)})
                continue
            except ModbusError:
                _FAILED_SPANS.add((fc, start, end))
        for b0, b1 in group:
            try:
                vals = cli.read_registers(fc, b0, b1 - b0 + 1)
                out.update({b0 + i: v for i, v in enumerate(vals)})
            except ModbusError:
                for a in range(b0, b1 + 1):
                    try:
                        out[a] = cli.read_registers(fc, a, 1)[0]
                    except ModbusError:
                        out[a] = None
    return out


def read_bit_range(cli, fc, first, last) -> dict[int, bool | None]:
    try:
        bits = cli.read_bits(fc, first, last - first + 1)
        return {first + i: b for i, b in enumerate(bits)}
    except ModbusError:
        out = {}
        for a in range(first, last + 1):
            try:
                out[a] = cli.read_bits(fc, a, 1)[0]
            except ModbusError:
                out[a] = None
        return out


def decode_table(tbl, raw: dict, m: dict) -> None:
    for addr, key, _d, typ, _u, enum in tbl:
        r = raw.get(addr)
        if enum is not None and r is not None and s16(r) in enum:
            m[key] = s16(r)  # enum: store the number, text in output/columns_meta
        else:
            m[key] = decode(r, typ)


def poll(cli: ReadOnlyModbusTCP, slow: bool) -> tuple[dict, dict]:
    """Reads all registers. slow=True: also holding registers + coils.
    Returns: (decoded values, raw values {"IR51": 70, ...})."""
    ir = read_reg_blocks(cli, FC_READ_INPUT, INPUT_BLOCKS)
    try:  # on the unit (MMI 4.2.0) 1-30 is returned without exception
        bits = cli.read_bits(FC_READ_DISCRETE, 1, 30)
        di = {1 + i: b for i, b in enumerate(bits)}
    except ModbusError:  # fallback: documented 1-25, the rest one by one
        di = read_bit_range(cli, FC_READ_DISCRETE, 1, 25)
        for a in range(26, 31):
            try:
                di[a] = cli.read_bits(FC_READ_DISCRETE, a, 1)[0]
            except ModbusError:
                di[a] = None

    m: dict = {}
    raw: dict = {f"IR{a}": v for a, v in ir.items()}
    raw.update({f"DI{a}": (None if v is None else int(v)) for a, v in di.items()})
    decode_table(INPUT_REGS, ir, m)
    m["flow_raw"] = ir.get(49)
    for addr, key, _d in DISCRETE_INPUTS:
        m[key] = di.get(addr)

    if slow:
        hr = read_reg_blocks(cli, FC_READ_HOLDING, HOLDING_BLOCKS)
        co = read_bit_range(cli, FC_READ_COILS, 1, 3)
        co.update(read_bit_range(cli, FC_READ_COILS, 4, 7))  # undocumented, separately
        raw.update({f"HR{a}": v for a, v in hr.items()})
        raw.update({f"CO{a}": (None if v is None else int(v)) for a, v in co.items()})
        decode_table(HOLDING_REGS, hr, m)
        for addr, key, _d in COILS:
            m[key] = co.get(addr)
    else:
        for _a, key, *_ in HOLDING_REGS:
            m[key] = None
        for _a, key, _d in COILS:
            m[key] = None
    return m, raw


# ===========================================================================
# 4) CALCULATED values (not provided by the heat pump!)
# ===========================================================================

CP_WATER = 4.186  # kJ/(kg·K), density ≈ 1 kg/l assumed


def q_kw(flow_lpm, t_out, t_in):
    if None in (flow_lpm, t_out, t_in):
        return None
    return round(flow_lpm / 60 * CP_WATER * (t_out - t_in), 3)


def fault_code(m: dict) -> str | None:
    """Error code as shown on the display, e.g. '7H-19'."""
    if m.get("fault") is None:
        return None
    if m["fault"] == 0:
        return "none"
    code = m.get("error_code") or "??"
    sub = m.get("error_subcode")
    return code if sub is None else f"{code}-{sub:02d}"


class Calculator:
    """Instantaneous values + energy integration (trapezoid) between two polls."""

    ENERGY_KEYS = ("e_el_kwh", "q_hp_kwh", "q_buh_kwh", "q_total_kwh")

    def __init__(self, buh_kw_per_relay: float | None, max_gap: float = 120.0,
                 hyd: "HydraulicAnalyzer | None" = None):
        self.buh_kw_per_relay = buh_kw_per_relay
        self.hyd = hyd
        self.max_gap = max_gap
        self.prev_t = None
        self.prev = None
        self.e = {k: 0.0 for k in self.ENERGY_KEYS}

    def compute(self, m: dict, now: float) -> dict:
        c: dict = {}
        f = m.get("flow")
        lwt_phe, lwt_buh, ret = m.get("t_lwt_phe"), m.get("t_lwt_buh"), m.get("t_return")
        c["fault_code"] = fault_code(m)
        c["dt_hp_k"] = round(lwt_phe - ret, 2) if None not in (lwt_phe, ret) else None
        c["dt_buh_k"] = round(lwt_buh - lwt_phe, 2) if None not in (lwt_buh, lwt_phe) else None
        c["q_hp_kw"] = q_kw(f, lwt_phe, ret)
        c["q_buh_kw"] = q_kw(f, lwt_buh, lwt_phe)
        c["q_total_kw"] = q_kw(f, lwt_buh, ret)
        p = m.get("p_el")
        c["cop_instant"] = (round(c["q_hp_kw"] / p, 2)
                             if c["q_hp_kw"] is not None and p and p > 0.05 else None)

        relays = [m.get(f"d_buh_r{i}") for i in range(1, 7)]
        c["buh_relays_on"] = None if None in relays else sum(relays)
        # suppress sensor noise: all relays off -> backup heater power 0
        if c["buh_relays_on"] == 0:
            c["q_buh_clean_kw"] = 0.0
        elif c["q_buh_kw"] is not None:
            c["q_buh_clean_kw"] = max(c["q_buh_kw"], 0.0)
        else:
            c["q_buh_clean_kw"] = None
        c["buh_kw_relay_estimate"] = (round(c["buh_relays_on"] * self.buh_kw_per_relay, 2)
                                         if self.buh_kw_per_relay is not None
                                         and c["buh_relays_on"] is not None else None)

        # energy (trapezoid) – backup heater with the cleaned value
        cur = {"e_el_kwh": p, "q_hp_kwh": c["q_hp_kw"],
               "q_buh_kwh": c["q_buh_clean_kw"], "q_total_kwh": c["q_total_kw"]}
        inc = {k: None for k in cur}
        c["dt_s"] = None
        if self.prev_t is not None and 0 < now - self.prev_t <= self.max_gap:
            dt = now - self.prev_t
            c["dt_s"] = round(dt, 2)
            for k, v in cur.items():
                pv = self.prev.get(k)
                if v is not None and pv is not None:
                    inc[k] = (v + pv) / 2 * dt / 3600
                    self.e[k] += inc[k]
        self.prev_t, self.prev = now, cur
        for k, v in inc.items():
            c["d" + k] = None if v is None else round(v, 6)
        for k, v in self.e.items():
            c[k] = round(v, 4)
        c["spf_since_start"] = (round(self.e["q_hp_kwh"] / self.e["e_el_kwh"], 2)
                              if self.e["e_el_kwh"] > 0.001 else None)
        if self.hyd is not None:
            c.update(self.hyd.compute(m, now))
        return c


# ===========================================================================
# 4b) Hydraulic separator – external sensors on the secondary side (optional, read-only)
# ===========================================================================
#
# The Altherma only knows its own side of the separator (primary): leaving
# water IR41, return IR42, flow IR49. Comparing with the secondary side
# (heating circuits) needs two additional sensors on the secondary supply and
# return. Sources (SEC_SUPPLY_SOURCE / SEC_RETURN_SOURCE):
#
#   w1:28-0123456789ab        1-Wire sensor (DS18B20) on the logger host
#   file:/path/to/file        file containing a number in °C (for own scripts)
#   modbus://host[:port]/unit/ir|hr/address[/scale]
#                             Modbus TCP, e.g. an ESP32. Only FC 0x03/0x04
#                             (ReadOnlyModbusTCP), address 1-based,
#                             signed 16 bit × scale (default 0.01)

class ExtSource:
    """An external temperature source. read() returns °C or raises an exception."""
    VALID = (-30.0, 110.0)

    def __init__(self, spec: str, offset: float = 0.0, w1_base: str = "/sys/bus/w1/devices",
                 timeout: float = 3.0):
        self.spec, self.offset, self.timeout = spec.strip(), offset, timeout
        if self.spec.startswith("w1:"):
            self.kind, self.path = "w1", os.path.join(w1_base, self.spec[3:].strip())
        elif self.spec.startswith("file:"):
            self.kind, self.path = "file", self.spec[5:].strip()
        elif self.spec.startswith("modbus://"):
            self.kind = "modbus"
            u = urllib.parse.urlparse(self.spec)
            parts = [p for p in u.path.split("/") if p]
            if len(parts) not in (3, 4) or parts[1].lower() not in ("ir", "hr"):
                raise ValueError(f"invalid Modbus source {spec!r} – format "
                                 "modbus://host[:port]/unit/ir|hr/address[/scale]")
            self.host, self.port = u.hostname, u.port or 502
            self.unit, self.addr = int(parts[0]), int(parts[2])
            self.fc = FC_READ_INPUT if parts[1].lower() == "ir" else FC_READ_HOLDING
            self.scale = float(parts[3]) if len(parts) == 4 else 0.01
        else:
            raise ValueError(f"unknown source {spec!r} (w1:…, file:…, modbus://…)")

    def _read_raw(self) -> float:
        if self.kind == "w1":
            t_file = os.path.join(self.path, "temperature")
            if os.path.exists(t_file):
                with open(t_file) as f:
                    milli = int(f.read().strip())
            else:
                with open(os.path.join(self.path, "w1_slave")) as f:
                    lines = f.read().splitlines()
                if not lines or not lines[0].strip().endswith("YES"):
                    raise ValueError("1-Wire CRC error")
                milli = int(lines[1].split("t=")[1])
            if milli == 85000:   # DS18B20 power-on value, not a real reading
                raise ValueError("1-Wire returns power-on value 85 °C")
            return milli / 1000
        if self.kind == "file":
            with open(self.path) as f:
                return float(f.read().strip().replace(",", "."))
        with ReadOnlyModbusTCP(self.host, self.port, self.unit, timeout=self.timeout) as cli:
            raw = cli.read_registers(self.fc, self.addr, 1)[0]
        if raw in SPECIAL_VALUES:
            raise ValueError(f"special value {raw}")
        return s16(raw) * self.scale

    def read(self) -> float:
        v = self._read_raw() + self.offset
        if not self.VALID[0] <= v <= self.VALID[1]:
            raise ValueError(f"implausible: {v:.1f} °C")
        return round(v, 2)


class ExternalTemps:
    """Reads the secondary sensors; errors -> NULL, logged only when the state changes."""
    KEYS = ("ext_t_sec_supply", "ext_t_sec_return")

    def __init__(self, sources: dict[str, ExtSource | None]):
        self.sources = {k: s for k, s in sources.items() if s is not None}
        self.last_err: dict[str, str | None] = {}

    @property
    def active(self) -> bool:
        return bool(self.sources)

    def read(self) -> dict:
        out = {k: None for k in self.KEYS} if self.sources else {}
        for k, src in self.sources.items():
            try:
                out[k] = src.read()
                err = None
            except Exception as e:  # noqa: BLE001 – a sensor error must not stop anything
                err = f"{type(e).__name__}: {e}"
            if err != self.last_err.get(k):
                log(f"sensor {k} ({src.spec}): " + (err or "OK again"))
                self.last_err[k] = err
        return out


EXT_INFO = [
    ("ext_t_sec_supply", "EXTERNAL: secondary supply (after separator)", "°C"),
    ("ext_t_sec_return", "EXTERNAL: secondary return (before separator)", "°C"),
]

SEP_TEXT = {
    "balanced": ("Flow rates match.", "Leave as is."),
    "primary_excess": (
        "The heat pump circulates more than the heating circuits take – hot water flows "
        "straight back through the separator (heat pump return clearly warmer than secondary return).",
        "Increase the ΔT setpoint ([1.14] / [2.14]) or lower the heat pump minimum flow."),
    "secondary_excess": (
        "The heating circuits circulate more than the heat pump – return water mixes into the "
        "supply (secondary supply colder than heat pump leaving water).",
        "First lower the pump speed of the heating circuits ([3.13.x]), "
        "then reduce the ΔT setpoint if needed."),
    "unclear": ("Pattern matches no case (sensor offset, heat losses or transition).",
                "Check the sensors or calibrate with SEC_SUPPLY_OFFSET / SEC_RETURN_OFFSET."),
}


class HydraulicAnalyzer:
    """Compares the primary and secondary side of the hydraulic separator.

    Evaluated only in stable operation: compressor on, no defrost, no DHW,
    flow ≥ flow_min, uninterrupted for settle_min minutes.
    Temperatures are averaged over avg_min minutes.

      ΔLWT = LWT HP (IR41) − secondary supply   ΔRET = return HP (IR42) − secondary return
      balanced:          |ΔLWT| ≤ tol  and |ΔRET| ≤ tol
      primary_excess:    |ΔLWT| ≤ tol  and  ΔRET > tol
      secondary_excess:   ΔLWT > tol   and |ΔRET| ≤ tol
      otherwise:         unclear

    Secondary flow (estimate, energy balance without losses):
      V̇_sec = V̇_HP × (LWT_HP − RET_HP) / (supply_sec − return_sec)

    Without secondary sensors there is only a weaker hint: if the Bizone mixing
    valve is fully open (IR71 ≥ 98 %), IR72 equals the supply after the mixer.
    calc_sep_hint_dlwt_k = IR41 − IR72. Only meaningful if the mixing kit sits
    downstream of the separator.
    """

    def __init__(self, tol: float = 1.0, avg_min: float = 5.0, settle_min: float = 10.0,
                 flow_min: float | None = 3.0):
        self.tol, self.avg_s, self.settle_s = tol, avg_min * 60, settle_min * 60
        self.flow_min = flow_min or 0.0
        self.buf: deque = deque()
        self.stable_since = None

    def _stable(self, m: dict) -> bool:
        comp = m.get("compressor") == 1 or m.get("d_compressor") is True
        defrost = bool(m.get("d_defrost")) or m.get("defrost") == 1
        dhw = m.get("three_way_valve") == 1 or bool(m.get("d_dhw"))
        flow = m.get("flow")
        return comp and not defrost and not dhw and flow is not None and flow >= self.flow_min

    @staticmethod
    def _flow_sec(flow, lwt_hp, ret_hp, lwt_s, ret_s):
        if None in (flow, lwt_hp, ret_hp, lwt_s, ret_s):
            return None
        dt_hp, dt_s = lwt_hp - ret_hp, lwt_s - ret_s
        if dt_hp < 0.5 or dt_s < 1.0:   # ΔT too small -> estimate meaningless
            return None
        return round(flow * dt_hp / dt_s, 1)

    def classify(self, dlwt: float, dret: float) -> str:
        t = self.tol
        if abs(dlwt) <= t and abs(dret) <= t:
            return "balanced"
        if abs(dlwt) <= t and dret > t:
            return "primary_excess"
        if dlwt > t and abs(dret) <= t:
            return "secondary_excess"
        return "unclear"

    def compute(self, m: dict, now: float) -> dict:
        c = {k: None for k in ("sep_dlwt_k", "sep_dret_k", "sep_dt_sec_k",
                               "sep_flow_sec_lpm", "sep_flow_ratio",
                               "sep_state", "sep_hint_dlwt_k")}
        lwt_hp, ret_hp, flow = m.get("t_lwt_buh"), m.get("t_return"), m.get("flow")
        lwt_s, ret_s = m.get("ext_t_sec_supply"), m.get("ext_t_sec_return")
        stable = self._stable(m)

        # hint without extra sensors (Bizone mixing valve fully open)
        mix, lwt_mix = m.get("mixing_valve"), m.get("t_lwt_mixed")
        if stable and None not in (mix, lwt_mix, lwt_hp) and mix >= 98:
            c["sep_hint_dlwt_k"] = round(lwt_hp - lwt_mix, 2)

        if None in (lwt_hp, ret_hp, lwt_s, ret_s):
            self.buf.clear()
            self.stable_since = None
            return c
        # instantaneous values (always)
        c["sep_dlwt_k"] = round(lwt_hp - lwt_s, 2)
        c["sep_dret_k"] = round(ret_hp - ret_s, 2)
        c["sep_dt_sec_k"] = round(lwt_s - ret_s, 2)
        if not stable:
            self.buf.clear()
            self.stable_since = None
            return c
        if self.stable_since is None:
            self.stable_since = now
        self.buf.append((now, lwt_hp, ret_hp, lwt_s, ret_s, flow))
        while self.buf and now - self.buf[0][0] > self.avg_s:
            self.buf.popleft()
        if now - self.stable_since < self.settle_s:
            return c                     # still settling -> no verdict
        n = len(self.buf)
        a_lwt, a_ret, a_lwt_s, a_ret_s, a_f = (sum(x[i] for x in self.buf) / n for i in range(1, 6))
        dlwt, dret = a_lwt - a_lwt_s, a_ret - a_ret_s
        c["sep_dlwt_k"], c["sep_dret_k"] = round(dlwt, 2), round(dret, 2)
        c["sep_dt_sec_k"] = round(a_lwt_s - a_ret_s, 2)
        c["sep_flow_sec_lpm"] = self._flow_sec(a_f, a_lwt, a_ret, a_lwt_s, a_ret_s)
        if c["sep_flow_sec_lpm"] is not None and a_f:
            c["sep_flow_ratio"] = round(c["sep_flow_sec_lpm"] / a_f, 2)
        c["sep_state"] = self.classify(dlwt, dret)
        return c


CALC_INFO = [
    ("fault_code",            "Error code as on the display (e.g. 7H-19)",  ""),
    ("dt_hp_k",               "ΔT heat pump (LWT PHE − return)",            "K"),
    ("dt_buh_k",              "ΔT backup heater (LWT BUH − LWT PHE)",       "K"),
    ("q_hp_kw",               "Heat output heat pump",                      "kW"),
    ("q_buh_kw",              "Heat output backup heater (raw, with noise)", "kW"),
    ("q_buh_clean_kw",        "Backup heater cleaned ≈ electrical use BUH", "kW"),
    ("q_total_kw",            "Heat output total",                          "kW"),
    ("cop_instant",           "Instantaneous COP (Q_HP / reg. 51)",         ""),
    ("buh_relays_on",         "Backup heater: number of active relays",     ""),
    ("buh_kw_relay_estimate", "Backup heater: relays × kW (rough estimate)", "kW"),
    ("dt_s",                  "Seconds since previous poll (NULL = gap)",   "s"),
    ("de_el_kwh",             "Electricity HP in interval (∫ reg. 51)",     "kWh"),
    ("dq_hp_kwh",             "Heat HP in interval",                        "kWh"),
    ("dq_buh_kwh",            "Backup heater in interval (cleaned)",        "kWh"),
    ("dq_total_kwh",          "Heat total in interval",                     "kWh"),
    ("e_el_kwh",              "Electricity HP since script start",          "kWh"),
    ("q_hp_kwh",              "Heat HP since script start",                 "kWh"),
    ("q_buh_kwh",             "Backup heater since script start (cleaned)", "kWh"),
    ("q_total_kwh",           "Heat total since script start",              "kWh"),
    ("spf_since_start",       "SPF since script start",                     ""),
    ("sep_dlwt_k",            "Separator: LWT HP − secondary supply (avg, if stable)", "K"),
    ("sep_dret_k",            "Separator: return HP − secondary return (avg, if stable)", "K"),
    ("sep_dt_sec_k",          "Separator: secondary ΔT",                    "K"),
    ("sep_flow_sec_lpm",      "Separator: secondary flow (estimate)",       "l/min"),
    ("sep_flow_ratio",        "Separator: flow secondary/primary",          ""),
    ("sep_state",             "Separator: verdict (stable operation only)", ""),
    ("sep_hint_dlwt_k",       "Separator hint without sensors: IR41 − IR72 (mixing valve open)", "K"),
]

DIAG_INFO = [
    ("diag_poll_ms",     "Poll duration",                                 "ms"),
    ("diag_requests",    "Modbus requests sent",                          ""),
    ("diag_exceptions",  "Modbus exceptions (gaps -> single reads)",      ""),
    ("diag_consec_fail", "Failed polls in a row (before this one)",       ""),
    ("diag_slow",        "Settings (HR/CO) read in this row",             "bool"),
    ("diag_error_kind",  "Error type: timeout/refused/network/modbus",    ""),
]


# ===========================================================================
# 5) Output
# ===========================================================================

def fmt(v, enum=None) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, bool):
        return "ON" if v else "off"
    if enum is not None and v in enum:
        return f"{enum[v]} ({v})"
    return str(v)


def print_report(ts: str, m: dict, c: dict, raw: dict) -> None:
    w = 44
    print(f"\n{'=' * 72}\n Daikin Altherma 4 – {ts}\n{'=' * 72}")

    def special(tag):
        r = raw.get(tag)
        return f"   [{SPECIAL_VALUES[r]}]" if r in SPECIAL_VALUES else ""

    print("\n### MEASURED – input registers (FC 0x04) ###")
    for addr, key, desc, _t, unit, enum in INPUT_REGS:
        extra = (f"   [raw value {fmt(m.get('flow_raw'))}, ×{FLOW_SCALE}]"
                 if key == "flow" else special(f"IR{addr}"))
        print(f"  IR{addr:<4} {desc:<{w}}: {fmt(m[key], enum)} {unit}{extra}".rstrip())

    print("\n### MEASURED – discrete inputs (FC 0x02) ###")
    for addr, key, desc in DISCRETE_INPUTS:
        print(f"  DI{addr:<4} {desc:<{w}}: {fmt(m[key])}")

    print("\n### SETTINGS – holding registers (FC 0x03, read only) ###")
    print("  Note: the Modbus interface is change-based – HR/CO can differ from the")
    print("  display (e.g. coils 'off' although zones are running).")
    for addr, key, desc, _t, unit, enum in HOLDING_REGS:
        print(f"  HR{addr:<4} {desc:<{w}}: {fmt(m[key], enum)} {unit}{special(f'HR{addr}')}".rstrip())

    print("\n### SETTINGS – coils (FC 0x01, read only) ###")
    for addr, key, desc in COILS:
        print(f"  CO{addr:<4} {desc:<{w}}: {fmt(m[key])}")

    if any(k in m for k, *_ in EXT_INFO):
        print("\n### EXTERNAL – additional sensors (not from the heat pump) ###")
        for key, desc, unit in EXT_INFO:
            print(f"  {desc:<{w + 7}}: {fmt(m.get(key))} {unit}".rstrip())

    print("\n" + "#" * 72)
    print("### [CALCULATED] – NOT from the heat pump, but by this script   ###")
    print("### Q = flow/60 × 4.186 × ΔT ; energy = trapezoid integral      ###")
    print("#" * 72)
    for key, desc, unit in CALC_INFO:
        print(f"  [CALCULATED] {desc:<{w}}: {fmt(c.get(key))} {unit}".rstrip())
    z = c.get("sep_state")
    instant = False
    if z is None and None not in (c.get("sep_dlwt_k"), c.get("sep_dret_k")):
        z, instant = HydraulicAnalyzer().classify(c["sep_dlwt_k"], c["sep_dret_k"]), True
    if z in SEP_TEXT:
        meaning, action = SEP_TEXT[z]
        note = (" (instantaneous, not averaged – only valid with the compressor running)"
                if instant else "")
        print(f"\n  Separator: {z}{note}\n  {meaning}\n  Action: {action}")


SEP_SHORT = {"balanced": "ok", "primary_excess": "HP>HC",
             "secondary_excess": "HC>HP", "unclear": "?"}


def status_line(ts: str, m: dict, c: dict, d: dict) -> str:
    def f(v, nd=2):
        if isinstance(v, bool):
            return str(int(v))
        return "n/a" if v is None else (f"{v:.{nd}f}" if isinstance(v, float) else str(v))
    return (f"{ts}  P_el={f(m.get('p_el'))} kW  Q_HP={f(c.get('q_hp_kw'))} kW  "
            f"Q_BUH={f(c.get('q_buh_clean_kw'))} kW  COP={f(c.get('cop_instant'))}  "
            f"LWT={f(m.get('t_lwt_buh'), 1)}/RET={f(m.get('t_return'), 1)} °C  "
            f"Ta={f(m.get('t_outdoor'), 1)} °C  p={f(m.get('water_pressure'))} bar  "
            f"Comp={f(m.get('compressor'))} Defrost={f(m.get('d_defrost'))}  "
            f"Error={f(c.get('fault_code'))}  "
            + (f"Sep={SEP_SHORT.get(c.get('sep_state'), '–')} "
               f"(ΔLWT={f(c.get('sep_dlwt_k'), 1)}/ΔRET={f(c.get('sep_dret_k'), 1)} K)  "
               if m.get("ext_t_sec_supply") is not None or m.get("ext_t_sec_return") is not None else "")
            + f"[{d.get('diag_poll_ms')} ms, "
            f"{d.get('diag_requests')} req{', +HR/CO' if d.get('diag_slow') else ''}]")


def scan(cli: ReadOnlyModbusTCP, max_addr: int) -> None:
    """Raw scan 1..max_addr for input/holding/discrete/coils – also shows undocumented addresses."""
    known = {
        "IR": {a for a, *_ in INPUT_REGS}, "HR": {a for a, *_ in HOLDING_REGS},
        "DI": {a for a, *_ in DISCRETE_INPUTS}, "CO": {a for a, *_ in COILS},
    }
    print(f"Raw scan of addresses 1..{max_addr} (* = not in register list)\n")
    for tag, fc in (("IR", FC_READ_INPUT), ("HR", FC_READ_HOLDING)):
        for a in range(1, max_addr + 1):
            try:
                v = cli.read_registers(fc, a, 1)[0]
            except ModbusError:
                continue
            mark = " " if a in known[tag] else "*"
            sp = f"  ({SPECIAL_VALUES[v]})" if v in SPECIAL_VALUES else ""
            print(f" {mark} {tag}{a:<5} raw=0x{v:04X}  u16={v:<6} s16={s16(v):<7} ×0.01={s16(v) / 100:.2f}{sp}")
    for tag, fc in (("DI", FC_READ_DISCRETE), ("CO", FC_READ_COILS)):
        for a in range(1, max_addr + 1):
            try:
                b = cli.read_bits(fc, a, 1)[0]
            except ModbusError:
                continue
            mark = " " if a in known[tag] else "*"
            print(f" {mark} {tag}{a:<5} {int(b)}")


# ===========================================================================
# 6) SQL schema (MySQL/MariaDB)
# ===========================================================================

TEXT_COLUMNS = {"error_code", "calc_fault_code", "calc_sep_state", "diag_error_kind", "raw_json"}


def column_meta() -> list[tuple]:
    """(name, source, address, description, unit, enum_json)"""
    def ej(enum):
        return json.dumps(enum, ensure_ascii=False) if enum else None
    meta = []
    for addr, key, desc, _t, unit, enum in INPUT_REGS:
        meta.append((key, "IR", addr, desc, unit, ej(enum)))
    meta.append(("flow_raw", "raw", 49, "Flow raw value IR49 (unscaled)", "", None))
    for addr, key, desc, _t, unit, enum in HOLDING_REGS:
        meta.append((key, "HR", addr, desc + " [only every --slow-interval s]", unit, ej(enum)))
    for addr, key, desc in DISCRETE_INPUTS:
        meta.append((key, "DI", addr, desc, "bool", None))
    for addr, key, desc in COILS:
        meta.append((key, "CO", addr, desc + " [only every --slow-interval s]", "bool", None))
    for key, desc, unit in EXT_INFO:
        meta.append((key, "ext", None, desc, unit, None))
    for key, desc, unit in CALC_INFO:
        meta.append(("calc_" + key, "calc", None, "[CALCULATED] " + desc, unit, None))
    for key, desc, unit in DIAG_INFO:
        meta.append((key, "diag", None, desc, unit, None))
    meta.append(("raw_json", "raw", None, "All raw values as JSON, e.g. {\"IR51\": 70}", "", None))
    return meta


VIEWS = {
    # Events from state changes (LAG). Gaps (ok=0) do not split runs – short
    # outages can extend a run; duration from calc_dt_s.
    "v_phase_base": """
        SELECT id, ts, ts_unix, calc_dt_s,
               compressor, d_defrost, calc_buh_relays_on, calc_dq_buh_kwh,
               LAG(compressor)  OVER w AS k_prev,
               LAG(d_defrost)   OVER w AS a_prev,
               LAG(calc_buh_relays_on > 0) OVER w AS b_prev
        FROM samples WHERE ok = 1
        WINDOW w AS (ORDER BY ts_unix)""",
    "v_compressor_starts": """
        WITH x AS (
          SELECT *, SUM(CASE WHEN compressor = 1 AND k_prev = 0 THEN 1 ELSE 0 END)
                    OVER (ORDER BY ts_unix) AS run_no
          FROM v_phase_base)
        SELECT run_no, MIN(ts) AS start, MAX(ts) AS finish,
               ROUND(SUM(CASE WHEN compressor = 1 THEN COALESCE(calc_dt_s, 0) END) / 60.0, 1) AS duration_min
        FROM x WHERE compressor = 1 AND run_no > 0
        GROUP BY run_no""",
    "v_defrosts": """
        WITH x AS (
          SELECT *, SUM(CASE WHEN d_defrost = 1 AND a_prev = 0 THEN 1 ELSE 0 END)
                    OVER (ORDER BY ts_unix) AS nr
          FROM v_phase_base)
        SELECT nr, MIN(ts) AS start, MAX(ts) AS finish,
               ROUND(SUM(CASE WHEN d_defrost = 1 THEN COALESCE(calc_dt_s, 0) END) / 60.0, 1) AS duration_min
        FROM x WHERE d_defrost = 1 AND nr > 0
        GROUP BY nr""",
    "v_backup_heater": """
        WITH x AS (
          SELECT *, SUM(CASE WHEN calc_buh_relays_on > 0 AND b_prev = 0 THEN 1 ELSE 0 END)
                    OVER (ORDER BY ts_unix) AS nr
          FROM v_phase_base)
        SELECT nr, MIN(ts) AS start, MAX(ts) AS finish, MAX(calc_buh_relays_on) AS max_relays,
               ROUND(SUM(CASE WHEN calc_buh_relays_on > 0 THEN COALESCE(calc_dt_s, 0) END) / 60.0, 1) AS duration_min,
               ROUND(SUM(calc_dq_buh_kwh), 3) AS kwh
        FROM x WHERE calc_buh_relays_on > 0 AND nr > 0
        GROUP BY nr""",
    "v_day": """
        WITH b AS (SELECT * FROM v_phase_base),
        s AS (SELECT DATE(ts) AS day, samples.* FROM samples)
        SELECT s.day,
               COUNT(*) AS n_samples,
               SUM(1 - s.ok) AS failures,
               ROUND(AVG(s.t_outdoor), 1) AS t_outdoor_avg,
               ROUND(SUM(s.calc_de_el_kwh), 2) AS el_kwh,
               ROUND(SUM(s.calc_dq_hp_kwh), 2) AS heat_hp_kwh,
               ROUND(SUM(s.calc_dq_buh_kwh), 2) AS backup_heater_kwh,
               ROUND(SUM(s.calc_dq_hp_kwh) / NULLIF(SUM(s.calc_de_el_kwh), 0), 2) AS spf,
               ROUND(SUM(CASE WHEN s.compressor = 1 THEN s.calc_dt_s END) / 3600.0, 2) AS compressor_h,
               (SELECT COUNT(*) FROM b WHERE date(b.ts) = s.day AND b.compressor = 1 AND b.k_prev = 0) AS compressor_starts,
               (SELECT COUNT(*) FROM b WHERE date(b.ts) = s.day AND b.d_defrost = 1 AND b.a_prev = 0) AS defrosts,
               MIN(s.water_pressure) AS pressure_min, MAX(s.water_pressure) AS pressure_max,
               ROUND(AVG(CASE WHEN s.compressor = 1 THEN s.calc_dt_hp_k END), 2) AS dt_hp_avg_k
        FROM s GROUP BY s.day""",
    "v_settings": """
        SELECT ts, s_lwt_heat_main, s_mode, s_space_heating, s_offset_heat_main,
               s_offset_heat_add, s_weather_heat_main, s_power_limit, s_dhw_mode,
               c_dhw, c_main_zone, c_add_zone,
               c_sched_heat_main, c_sched_cool_main, c_sched_heat_add, c_sched_cool_add
        FROM samples WHERE ok = 1 AND diag_slow = 1""",
}




def error_kind(e: Exception) -> str:
    if isinstance(e, (socket.timeout, TimeoutError)):
        return "timeout"
    if isinstance(e, ConnectionRefusedError):
        return "refused"
    if isinstance(e, ModbusError):
        return "modbus"
    return "network"


# ===========================================================================
# 7) Output targets (sinks): MySQL/MariaDB, InfluxDB 2
#    On outage each target buffers up to --buffer rows in memory and writes
#    them once the database is reachable again. An outage of one target blocks
#    neither the polling nor the other targets.
# ===========================================================================

def log(msg: str) -> None:
    print(f"{datetime.now():%Y-%m-%dT%H:%M:%S}  {msg}", file=sys.stderr, flush=True)


class Row:
    __slots__ = ("ts", "now", "ok", "error", "values")

    def __init__(self, ts: str, now: float, ok: bool, error: str | None, values: dict):
        self.ts, self.now, self.ok, self.error, self.values = ts, now, ok, error, values


class BufferedSink:
    name = "sink"

    def __init__(self, buffer: int):
        self.queue: deque[Row] = deque(maxlen=buffer)
        self.failing = False
        self.failing_since = None
        self.dropped = 0

    def write(self, row: Row) -> None:
        if len(self.queue) == self.queue.maxlen:
            self.dropped += 1
        self.queue.append(row)
        try:
            self.flush()
            if self.failing:
                log(f"{self.name}: reachable again, buffer written"
                    + (f" ({self.dropped} rows lost because the buffer was full)" if self.dropped else ""))
                self.failing, self.dropped, self.failing_since = False, 0, None
        except Exception as e:  # noqa: BLE001 – any target may fail
            self.reset()
            if not self.failing:
                log(f"{self.name}: write error, buffering ({e})")
                self.failing = True
                self.failing_since = time.time()

    def flush(self) -> None:
        raise NotImplementedError

    def reset(self) -> None:
        pass

    def close(self) -> None:
        pass


def _q(name: str, quote: str) -> str:
    return f"{quote}{name}{quote}"


def _db_value(v):
    return int(v) if isinstance(v, bool) else v


def sql_type(name: str, source: str) -> str:
    if name == "raw_json":
        return "JSON"
    if name in TEXT_COLUMNS:
        return "VARCHAR(32)"
    if source in ("DI", "CO") or name.startswith("diag_"):
        return "INT"
    return "DOUBLE"


# ----------------------------------------------------------------- MySQL ----

class MySQLSink(BufferedSink):
    """MySQL 8.x / MariaDB 10.6+. Table/columns/views are created automatically."""
    name = "MySQL"
    BATCH = 200

    def __init__(self, host: str, port: int, user: str, password: str, database: str, buffer: int):
        super().__init__(buffer)
        import pymysql  # only needed if MySQL is enabled
        self.pymysql = pymysql
        self.params = dict(host=host, port=port, user=user, password=password, database=database,
                           autocommit=False, connect_timeout=5, read_timeout=30, write_timeout=30,
                           charset="utf8mb4")
        self.con = None

    def _connect(self):
        if self.con is None:
            self.con = self.pymysql.connect(**self.params)
            self._ensure_schema()
        return self.con

    def _ensure_schema(self) -> None:
        with self.con.cursor() as cur:
            cur.execute("""CREATE TABLE IF NOT EXISTS samples (
                             id BIGINT AUTO_INCREMENT PRIMARY KEY,
                             ts DATETIME NOT NULL, ts_unix DOUBLE NOT NULL,
                             ok TINYINT NOT NULL, error TEXT,
                             INDEX idx_samples_ts (ts_unix), INDEX idx_samples_tsd (ts))""")
            cur.execute("""CREATE TABLE IF NOT EXISTS columns_meta (
                             name VARCHAR(64) PRIMARY KEY, source VARCHAR(8), address INT,
                             description VARCHAR(255), unit VARCHAR(16), enum_json TEXT)""")
            cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'samples'")
            existing = {r[0] for r in cur.fetchall()}
            missing = [(n, q) for n, q, *_ in column_meta() if n not in existing]
            if missing:
                cur.execute("ALTER TABLE samples " + ", ".join(
                    f"ADD COLUMN `{n}` {sql_type(n, q)}" for n, q in missing))
            cur.executemany("REPLACE INTO columns_meta VALUES (%s,%s,%s,%s,%s,%s)", column_meta())
            cur.execute("""CREATE TABLE IF NOT EXISTS logger_state (
                             k VARCHAR(64) PRIMARY KEY, v VARCHAR(255),
                             changed TIMESTAMP DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP)""")
            for name in reversed(list(VIEWS)):
                cur.execute(f"DROP VIEW IF EXISTS {name}")
            for name, sql in VIEWS.items():
                try:
                    cur.execute(f"CREATE VIEW {name} AS {sql}")
                except self.pymysql.MySQLError as e:  # views are a convenience, not required
                    log(f"MySQL: view {name} not created ({e})")
        self.con.commit()

    def flush(self) -> None:
        con = self._connect()
        while self.queue:
            batch = list(self.queue)[: self.BATCH]
            with con.cursor() as cur:
                for r in batch:
                    cols = ["ts", "ts_unix", "ok", "error"] + list(r.values.keys())
                    vals = [datetime.fromtimestamp(r.now).replace(microsecond=0), r.now,
                            int(r.ok), r.error] + [_db_value(v) for v in r.values.values()]
                    cur.execute(f'INSERT INTO samples ({",".join(_q(c, "`") for c in cols)}) '
                                f'VALUES ({",".join(["%s"] * len(cols))})', vals)
            con.commit()
            for _ in batch:
                self.queue.popleft()

    def reset(self) -> None:
        if self.con is not None:
            try:
                self.con.close()
            except Exception:  # noqa: BLE001
                pass
        self.con = None

    def close(self) -> None:
        self.reset()


# -------------------------------------------------------------- InfluxDB ----

def _lp_key(s: str) -> str:
    return s.replace("\\", "\\\\").replace(",", r"\,").replace("=", r"\=").replace(" ", r"\ ")


def _lp_str(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def line_protocol(measurement: str, tags: dict, row: Row) -> str | None:
    """All numbers as float (no type conflicts in Influx), bools as 0/1,
    text as string fields. raw_json is not written to Influx."""
    fields = {"ok": 1.0 if row.ok else 0.0}
    for k, v in row.values.items():
        if v is None or k == "raw_json":
            continue
        if isinstance(v, bool):
            fields[k] = 1.0 if v else 0.0
        elif isinstance(v, (int, float)):
            fields[k] = float(v)
        else:
            fields[k] = str(v)
    if row.error:
        fields["error"] = row.error
    parts = []
    for k, v in fields.items():
        parts.append(f"{_lp_key(k)}={_lp_str(v) if isinstance(v, str) else repr(v)}")
    tag_str = "".join(f",{_lp_key(k)}={_lp_key(v)}" for k, v in tags.items() if v)
    return f"{_lp_key(measurement)}{tag_str} {','.join(parts)} {int(row.now)}"


class InfluxSink(BufferedSink):
    """InfluxDB 2.x via the HTTP API /api/v2/write (no extra library)."""
    name = "InfluxDB"
    BATCH = 500

    def __init__(self, url: str, token: str, org: str, bucket: str, measurement: str,
                 tags: dict, buffer: int):
        super().__init__(buffer)
        q = urllib.parse.urlencode({"org": org, "bucket": bucket, "precision": "s"})
        self.endpoint = f"{url.rstrip('/')}/api/v2/write?{q}"
        self.token, self.measurement, self.tags = token, measurement, tags
        # ignore proxy environment variables: Influx is local/in the Docker network
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def flush(self) -> None:
        while self.queue:
            batch = list(self.queue)[: self.BATCH]
            body = "\n".join(line_protocol(self.measurement, self.tags, r) for r in batch)
            req = urllib.request.Request(self.endpoint, data=body.encode(), method="POST", headers={
                "Authorization": f"Token {self.token}",
                "Content-Type": "text/plain; charset=utf-8"})
            try:
                with self.opener.open(req, timeout=10) as resp:
                    if resp.status not in (200, 204):
                        raise RuntimeError(f"HTTP {resp.status}")
            except urllib.error.HTTPError as e:
                detail = e.read(300).decode("utf-8", "replace")
                if e.code == 400:  # data error: drop the batch instead of looping forever
                    log(f"InfluxDB: batch dropped (HTTP 400): {detail}")
                else:
                    raise RuntimeError(f"HTTP {e.code}: {detail}") from e
            for _ in batch:
                self.queue.popleft()


# ===========================================================================
# 7b) Notifications (Telegram, e-mail)
#     Sending runs in a background thread with retries, so that a slow mail
#     server does not delay polling. Messages are sent when an alarm triggers,
#     when it changes (e.g. a different error code), when it clears and
#     optionally as a reminder while the condition persists.
# ===========================================================================

class TelegramChannel:
    name = "Telegram"

    def __init__(self, token: str, chat_ids: list[str], api_url: str):
        self.url = f"{api_url.rstrip('/')}/bot{token}/sendMessage"
        self.chat_ids = chat_ids

    def send(self, subject: str, text: str) -> None:
        for chat_id in self.chat_ids:
            data = urllib.parse.urlencode({"chat_id": chat_id, "text": f"{subject}\n\n{text}",
                                           "disable_web_page_preview": "true"}).encode()
            req = urllib.request.Request(self.url, data=data, method="POST")
            with urllib.request.urlopen(req, timeout=15) as resp:
                body = json.loads(resp.read().decode() or "{}")
                if not body.get("ok", False):
                    raise RuntimeError(f"Telegram response: {body}")


class EmailChannel:
    name = "e-mail"

    def __init__(self, host: str, port: int, user: str | None, password: str | None,
                 sender: str, recipients: list[str], security: str):
        self.host, self.port, self.user, self.password = host, port, user, password
        self.sender, self.recipients, self.security = sender, recipients, security

    def send(self, subject: str, text: str) -> None:
        msg = EmailMessage()
        msg["Subject"] = subject
        msg["From"] = self.sender
        msg["To"] = ", ".join(self.recipients)
        msg["Date"] = email.utils.formatdate(localtime=True)
        msg["Message-ID"] = email.utils.make_msgid(domain="altherma4.local")
        msg.set_content(text)
        ctx = ssl.create_default_context()
        if self.security == "ssl":
            server = smtplib.SMTP_SSL(self.host, self.port, timeout=20, context=ctx)
        else:
            server = smtplib.SMTP(self.host, self.port, timeout=20)
        try:
            server.ehlo()
            if self.security == "starttls":
                server.starttls(context=ctx)
                server.ehlo()
            if self.user:
                server.login(self.user, self.password or "")
            server.send_message(msg)
        finally:
            try:
                server.quit()
            except Exception:  # noqa: BLE001
                pass


class Notifier:
    """Sends messages over all channels in a background thread."""
    RETRIES = (0, 30, 120, 600)  # seconds to wait before attempt 1..4

    def __init__(self, channels: list):
        self.channels = channels
        self.q: "queue.Queue[tuple[str, str] | None]" = queue.Queue(maxsize=200)
        self.thread = threading.Thread(target=self._run, name="notifier", daemon=True)
        if channels:
            self.thread.start()

    def notify(self, subject: str, text: str) -> None:
        if not self.channels:
            return
        log(f"notification: {subject}")
        try:
            self.q.put_nowait((subject, text))
        except queue.Full:
            log("notification dropped (queue full)")

    def _run(self) -> None:
        while True:
            item = self.q.get()
            if item is None:
                return
            subject, text = item
            for ch in self.channels:
                for i, wait in enumerate(self.RETRIES):
                    if wait:
                        time.sleep(wait)
                    try:
                        ch.send(subject, text)
                        break
                    except Exception as e:  # noqa: BLE001
                        last = i == len(self.RETRIES) - 1
                        log(f"{ch.name}: sending failed (attempt {i + 1}"
                            f"{', giving up' if last else ''}): {e}")

    def close(self, timeout: float = 20.0) -> None:
        """On shutdown: give pending messages a short time to be sent."""
        if self.channels and self.thread.is_alive():
            try:
                self.q.put_nowait(None)
            except queue.Full:
                pass
            self.thread.join(timeout)


class AlertState:
    def __init__(self):
        self.hits = 0          # consecutive polls with the condition met
        self.misses = 0        # consecutive polls without
        self.active = False    # reported
        self.since = None
        self.detail = None
        self.last_sent = 0.0


class AlertMonitor:
    """Checks all alarm conditions after every poll and reports state changes.

    Each condition yields (active?, detail text, confirmations-on, confirmations-off).
    Time-based conditions (e.g. "backup heater > 10 min") debounce via the
    duration itself; state conditions via ALERT_CONFIRM polls in a row.
    """
    LABELS = {
        "fault": "Fault", "emergency": "Emergency operation", "offline": "No connection",
        "pressure": "Water pressure low", "pressure_drop": "Pressure drop",
        "backup_heater": "Backup heater running", "short_cycling": "Compressor short cycling",
        "lwt_low": "Leaving water below setpoint", "flow": "Flow too low",
        "defrost_long": "Defrost takes long", "defrost_frequent": "Frequent defrosts",
        "frost_protection": "Frost protection active with cold water", "disinfection": "Disinfection failed",
        "sep_primary": "Separator: heat pump circulates more than the heating circuits",
        "sep_secondary": "Separator: heating circuits circulate more than the heat pump",
    }
    CRITICAL = {"fault", "emergency", "offline", "flow"}
    FLOW_HOLD_S = 120        # flow must be too low for this long
    P_SAMPLE_S = 300         # sample spacing of the pressure window

    def __init__(self, notifier: Notifier, a):
        self.n, self.a = notifier, a
        self.name = a.alert_name
        self.confirm = max(1, a.alert_confirm)
        self.repeat_s = a.alert_repeat_hours * 3600 if a.alert_repeat_hours > 0 else None
        self.states: dict[str, AlertState] = {}
        self._t: dict[str, float] = {}                      # start time "raw condition met"
        self.offline_since = None
        self.comp_starts: deque[float] = deque()            # times of compressor starts
        self.defrost_starts: deque[float] = deque()
        self.prev_comp = self.prev_defrost = None
        self.p_window: deque[tuple[float, float, float]] = deque()  # (t, p, pump speed %)
        self.p_last_add = 0.0
        self.p_recent: deque[float] = deque(maxlen=8)        # smoothing of the current pressure
        self.p_band: deque[tuple[float, float]] = deque(maxlen=4)  # recent (pump %, p) for the drop check
        self.buh_day, self.buh_day_kwh, self.buh_day_sent = None, 0.0, False

    # -- helpers ------------------------------------------------------------
    def _held(self, key: str, raw: bool, now: float) -> float:
        """How long has the raw condition been met without interruption (s)?"""
        if raw:
            self._t.setdefault(key, now)
            return now - self._t[key]
        self._t.pop(key, None)
        return 0.0

    @staticmethod
    def _count_recent(dq: deque, now: float, window: float) -> int:
        while dq and now - dq[0] > window:
            dq.popleft()
        return len(dq)

    # -- evaluation ------------------------------------------------------
    def update(self, ok: bool, m: dict | None, c: dict | None, now: float, sinks: list) -> None:
        a = self.a
        conds: dict[str, tuple] = {}   # key -> (active|None, detail, confirm_on, confirm_off)
        C = self.confirm

        if ok:
            self.offline_since = None
            conds["offline"] = (False, None, 1, 1)
            comp = m.get("compressor") == 1 or m.get("d_compressor") is True
            defrost = bool(m.get("d_defrost")) or m.get("defrost") == 1
            dhw = m.get("three_way_valve") == 1 or bool(m.get("d_dhw"))
            hot_start = bool(m.get("d_hot_start")) or m.get("hot_start") == 1

            # fault / warning (IR21) with code
            st = m.get("fault")
            if st is None:
                conds["fault"] = (None, None, C, C)
            else:
                alarm = st == 1 or (st == 2 and a.alert_warnings)
                kind = {1: "error", 2: "warning"}.get(st, str(st))
                conds["fault"] = (alarm, f"{c.get('fault_code')} ({kind})" if alarm else None, C, C)

            em = m.get("d_emergency")
            conds["emergency"] = (None if em is None else bool(em), None, C, C)

            # water pressure: threshold + slow drop (24 h window)
            # The sensor sits on the pressure side of the circulation pump: the reading
            # rises by ~0.6 bar between 30 % and 85 % pump speed, while the water
            # temperature hardly matters. The drop check therefore only compares
            # readings taken at the same pump speed (± ALERT_PRESSURE_PUMP_TOL %).
            p, pump = m.get("water_pressure"), m.get("pump_speed")
            if p is not None:
                self.p_recent.append(p)
                p_now = sum(self.p_recent) / len(self.p_recent)
                if a.alert_pressure_min is not None:
                    # hysteresis: clear only 0.1 bar above the threshold
                    was = self.states.get("pressure")
                    limit = a.alert_pressure_min + (0.1 if was and was.active else 0.0)
                    conds["pressure"] = (p_now < limit, f"{p_now:.2f} bar", C, C)
                if a.alert_pressure_drop and a.alert_pressure_drop > 0 and pump is not None:
                    tol = getattr(a, "alert_pressure_pump_tol", 5.0)
                    win = a.alert_pressure_drop_hours * 3600
                    # current value: mean of the last readings at (about) this pump speed
                    self.p_band.append((pump, p))
                    cur = [pp for (pu, pp) in self.p_band if abs(pu - pump) <= tol]
                    p_cur = sum(cur) / len(cur)
                    if now - self.p_last_add >= self.P_SAMPLE_S:
                        self.p_window.append((now, p, pump))
                        self.p_last_add = now
                    while self.p_window and now - self.p_window[0][0] > win:
                        self.p_window.popleft()
                    ref = [(t, pp) for (t, pp, pu) in self.p_window if abs(pu - pump) <= tol]
                    # need several comparable readings that reach back at least half the window
                    if len(ref) >= 3 and now - ref[0][0] >= win * 0.5:
                        drop = max(pp for _t, pp in ref) - p_cur
                        # hysteresis: once triggered, clear only below half the threshold
                        was = self.states.get("pressure_drop")
                        limit = a.alert_pressure_drop / 2 if was and was.active else a.alert_pressure_drop
                        conds["pressure_drop"] = (drop >= limit,
                                                f"−{drop:.2f} bar in {a.alert_pressure_drop_hours:g} h "
                                                f"at pump {pump:.0f} % (now {p_cur:.2f} bar)", C, C)

            # backup heater runs longer than X min (defrost excluded)
            relays = c.get("buh_relays_on")
            if relays is not None:
                dur = self._held("buh", relays > 0, now)
                conds["backup_heater"] = (relays > 0 and not defrost and dur >= a.alert_buh_min * 60,
                                     f"for {dur / 60:.0f} min, {relays} relay(s), "
                                     f"≈{(c.get('q_buh_clean_kw') or 0):.1f} kW", 1, C)
            # backup heater energy per day (one message per day)
            day = datetime.fromtimestamp(now).date()
            if day != self.buh_day:
                self.buh_day, self.buh_day_kwh, self.buh_day_sent = day, 0.0, False
            self.buh_day_kwh += c.get("dq_buh_kwh") or 0.0
            if (a.alert_buh_kwh_day and not self.buh_day_sent
                    and self.buh_day_kwh >= a.alert_buh_kwh_day):
                self.buh_day_sent = True
                self.n.notify(f"⚠️ {self.name}: backup heater {self.buh_day_kwh:.1f} kWh today",
                              f"The backup heater has already used {self.buh_day_kwh:.1f} kWh today "
                              f"(threshold {a.alert_buh_kwh_day:g} kWh).\n\n— Altherma 4 logger")

            # compressor starts per hour
            if self.prev_comp is False and comp:
                self.comp_starts.append(now)
            self.prev_comp = comp
            n_st = self._count_recent(self.comp_starts, now, 3600)
            if a.alert_starts_per_hour:
                conds["short_cycling"] = (n_st > a.alert_starts_per_hour, f"{n_st} starts in 60 min", 1, C)

            # leaving water persistently below setpoint (heating only, compressor on, no DHW/defrost)
            targets = [z for z in (m.get("lwt_target_main"), m.get("lwt_target_add")) if z is not None]
            if targets and lwt is not None and a.alert_lwt_dev:
                target = max(targets)
                raw = comp and not defrost and not dhw and not hot_start and lwt < target - a.alert_lwt_dev
                dur = self._held("lwt_low", raw, now)
                conds["lwt_low"] = (raw and dur >= a.alert_lwt_min * 60,
                                    f"leaving water {lwt:.1f} °C, target {target:.1f} °C, for {dur / 60:.0f} min", 1, C)

            # flow too low while the pump is running
            flow = m.get("flow")
            pump = m.get("circ_pump") == 1 or bool(m.get("d_circ_pump"))
            if flow is not None and a.alert_flow_min:
                raw = pump and not defrost and flow < a.alert_flow_min
                dur = self._held("flow", raw, now)
                conds["flow"] = (raw and dur >= self.FLOW_HOLD_S, f"{flow:.1f} l/min for {dur / 60:.0f} min", 1, C)

            # defrost: duration and frequency
            if self.prev_defrost is False and defrost:
                self.defrost_starts.append(now)
            self.prev_defrost = defrost
            dur = self._held("defrost", defrost, now)
            if a.alert_defrost_max_min:
                conds["defrost_long"] = (dur >= a.alert_defrost_max_min * 60, f"for {dur / 60:.0f} min", 1, 1)
            n_def = self._count_recent(self.defrost_starts, now, 3600)
            if a.alert_defrost_per_hour:
                conds["defrost_frequent"] = (n_def > a.alert_defrost_per_hour, f"{n_def} defrosts in 60 min", 1, C)

            # Frost protection (DI14 room, DI15 water pipe) is a normal protective
            # function: the unit switches it on by itself below ~6 °C outdoor.
            # Only report it (optional, ALERT_FROST=1) when the water is actually cold.
            fr, rf = m.get("d_frost_protection"), m.get("d_pipe_freeze")
            if getattr(a, "alert_frost", False) and (fr is not None or rf is not None):
                which = [n for n, v in (("room", fr), ("pipe", rf)) if v]
                temps = [t for t in (m.get("t_lwt_buh"), m.get("t_return")) if t is not None]
                cold = bool(temps) and min(temps) < getattr(a, "alert_frost_water_min", 10.0)
                conds["frost_protection"] = (bool(which) and cold,
                                             f"{'/'.join(which)}, water {min(temps):.1f} °C" if which and temps else None,
                                             C, C)

            # disinfection failed (IR63 = 0)
            ds = m.get("disinfection_status")
            if ds is not None:
                conds["disinfection"] = (ds == 0, None, C, C)

            # Hydraulic separator: imbalance over a longer time (stable operation only).
            # Polls that cannot be evaluated (compressor off, defrost, unclear) change nothing.
            z = c.get("sep_state")
            hyd_min = getattr(a, "alert_hyd_min", 0)
            if hyd_min and z in ("balanced", "primary_excess", "secondary_excess"):
                off = max(C, int(getattr(a, "alert_hyd_clear_min", 30) * 60 / max(getattr(a, "interval", 15), 1)))
                def g(k, nd=1):
                    v = c.get(k)
                    return "n/a" if v is None else f"{v:.{nd}f}"
                for key, st in (("sep_primary", "primary_excess"),
                                ("sep_secondary", "secondary_excess")):
                    dur = self._held(key, z == st, now)
                    conds[key] = (z == st and dur >= hyd_min * 60,
                                  f"ΔLWT {g('sep_dlwt_k')} K, ΔRET {g('sep_dret_k')} K, "
                                  f"flow HP {m.get('flow') or 0:.1f} / secondary ≈ "
                                  f"{g('sep_flow_sec_lpm')} l/min", 1, off)
        else:
            if self.offline_since is None:
                self.offline_since = now
            down = now - self.offline_since
            conds["offline"] = (down >= self.a.alert_offline_min * 60, f"for {down / 60:.0f} min", 1, 1)
            # all other states stay until data arrives again

        # databases (output targets)
        for s in sinks:
            key = f"db_{s.name}"
            self.LABELS.setdefault(key, f"database {s.name} not writing")
            fail_s = now - s.failing_since if s.failing and s.failing_since else 0.0
            fill = len(s.queue) / s.queue.maxlen if s.queue.maxlen else 0.0
            conds[key] = (fail_s >= self.a.alert_db_min * 60 or fill >= 0.8,
                          f"for {fail_s / 60:.0f} min, buffer {len(s.queue)}/{s.queue.maxlen}", 1, 1)

        for key, (active, detail, c_on, c_off) in conds.items():
            if active is not None:
                self._step(key, active, detail, m, c, now, c_on, c_off)

    def _step(self, key, active, detail, m, c, now, c_on, c_off) -> None:
        s = self.states.setdefault(key, AlertState())
        if active:
            s.hits += 1
            s.misses = 0
            if not s.active and s.hits >= c_on:
                s.active, s.since, s.detail, s.last_sent = True, now, detail, now
                self._send(key, "triggered", detail, m, c, now, s)
            elif s.active and key == "fault" and detail != s.detail:
                s.detail, s.last_sent = detail, now
                self._send(key, "changed", detail, m, c, now, s)
            elif s.active and self.repeat_s and now - s.last_sent >= self.repeat_s:
                s.last_sent = now
                self._send(key, "still active", detail or s.detail, m, c, now, s)
        else:
            s.misses += 1
            s.hits = 0
            if s.active and s.misses >= c_off:
                self._send(key, "cleared", s.detail, m, c, now, s)
                s.active, s.since, s.detail = False, None, None

    # -- message -----------------------------------------------------------
    def _send(self, key, what, detail, m, c, now, s) -> None:
        label = self.LABELS.get(key, key)
        icon = "✅" if what == "cleared" else ("🔴" if key in self.CRITICAL else "⚠️")
        subject = f"{icon} {self.name}: {label} {what}" + (f" – {detail}" if detail and what != "cleared" else "")
        lines = [f"Time: {datetime.fromtimestamp(now):%Y-%m-%d %H:%M:%S}",
                 f"Event: {label} {what}" + (f" – {detail}" if detail else "")]
        if s.since and what != "triggered":
            lines.append(f"Duration: {(now - s.since) / 60:.0f} min (since {datetime.fromtimestamp(s.since):%Y-%m-%d %H:%M})")
        if m:
            def g(k, unit="", nd=1):
                v = m.get(k)
                return "n/a" if v is None else (f"{v:.{nd}f}{unit}" if isinstance(v, float) else f"{v}{unit}")
            mode = {0: "stop", 1: "tank charging", 2: "heating", 3: "cooling", 4: "actuator"}.get(m.get("unit_mode"), "n/a")
            lines += ["",
                      f"Operation: {mode}, compressor {'on' if m.get('compressor') else 'off'}, "
                      f"defrost {'yes' if m.get('d_defrost') else 'no'}, emergency {'yes' if m.get('d_emergency') else 'no'}",
                      f"Outdoor {g('t_outdoor', ' °C')}, leaving water {g('t_lwt_buh', ' °C')} (target {g('lwt_target_add', ' °C')}), "
                      f"return {g('t_return', ' °C')}",
                      f"DHW {g('t_dhw', ' °C')}, pressure {g('water_pressure', ' bar', 2)}, "
                      f"flow {g('flow', ' l/min')}",
                      f"Electrical power {g('p_el', ' kW', 2)}, backup heater relays {c.get('buh_relays_on') if c else 'n/a'}",
                      *([f"Secondary: supply {g('ext_t_sec_supply', ' °C')}, return {g('ext_t_sec_return', ' °C')}"]
                        if m.get("ext_t_sec_supply") is not None or m.get("ext_t_sec_return") is not None else []),
                      f"Fault counter (IR82): {g('fault_counter')}"]
        hints = {
            "fault": "Look up the code in the Daikin error code list or on the user interface.",
            "offline": "Possible causes: indoor unit without power, network, Modbus disabled, restart.",
            "backup_heater": "Mind the phase load (a single-phase connection can trip the main breaker); check heating curve/heat pump output.",
            "pressure_drop": "Possible leak or venting – check the pressure on the gauge, refill if needed.",
            "flow": "Possible causes: air in the system, dirty filter, closed valve.",
            "short_cycling": "Frequent cycling costs efficiency – check heating curve/hydraulics/on-off hysteresis.",
            "lwt_low": "The heat pump does not reach the setpoint – check output, heating curve or hydraulics.",
            "defrost_long": "Check the evaporator (icing, condensate drain, air circulation).",
            "defrost_frequent": "Check the evaporator (icing, condensate drain, air circulation).",
            "sep_primary": "\n".join(SEP_TEXT["primary_excess"]).replace("\n", "\nAction: ", 1),
            "sep_secondary": "\n".join(SEP_TEXT["secondary_excess"]).replace("\n", "\nAction: ", 1),
        }
        if what != "cleared" and key in hints:
            lines += ["", hints[key]]
        lines += ["", "— Altherma 4 logger (read-only)"]
        self.n.notify(subject, "\n".join(lines))


# ---------------------------------------------------------- dead man's switch --

class Heartbeat:
    """Calls a URL regularly (e.g. healthchecks.io). If the call stops, the
    external service raises the alarm – even if the logger or the Pi is down."""

    def __init__(self, url: str | None, interval_min: float):
        self.url, self.interval = url, max(60.0, interval_min * 60)
        self.last = 0.0
        self.failing = False

    def tick(self, now: float) -> None:
        if not self.url or now - self.last < self.interval:
            return
        self.last = now
        threading.Thread(target=self._ping, daemon=True, name="heartbeat").start()

    def _ping(self) -> None:
        try:
            with urllib.request.urlopen(self.url, timeout=15) as resp:
                resp.read(64)
            if self.failing:
                log("heartbeat: successful again")
            self.failing = False
        except Exception as e:  # noqa: BLE001
            if not self.failing:
                log(f"heartbeat failed: {e}")
            self.failing = True


# --------------------------------------------------------------- weekly report --

def _f(v, nd=1, unit=""):
    return "–" if v is None else f"{v:.{nd}f}{unit}"


def mysql_connect(params: dict):
    import pymysql
    return pymysql.connect(**params)


def separator_section(rows, hint) -> list[str]:
    """Weekly report section on the hydraulic separator."""
    L = ["HYDRAULIC SEPARATOR (stable operation only)"]
    rows = [(z, float(h or 0), dl, dr, fp, fs) for z, h, dl, dr, fp, fs in rows]
    total = sum(r[1] for r in rows)
    if total <= 0:
        L.append("  No evaluation (no secondary sensors or compressor never stable).")
    else:
        order = ["balanced", "primary_excess", "secondary_excess", "unclear"]
        rows.sort(key=lambda r: order.index(r[0]) if r[0] in order else 9)
        for z, h, dl, dr, fp, fs in rows:
            L.append(f"  {z:<22} {h / total * 100:5.1f} % ({_f(h, 1, ' h')}), ΔLWT {_f(dl, 1)} K, "
                     f"ΔRET {_f(dr, 1)} K, flow HP {_f(fp, 1)} / sec ≈ {_f(fs, 1)} l/min")
        top = max(rows, key=lambda r: r[1])[0]
        if top in SEP_TEXT and top != "balanced":
            L.append(f"  Mostly: {SEP_TEXT[top][0]}")
            L.append(f"  Action: {SEP_TEXT[top][1]}")
    if hint and hint[1]:
        L.append(f"  Hint without sensors (IR41 − IR72, mixing valve open): avg {_f(hint[0], 1)} K "
                 f"from {hint[1]} polls")
    return L


def weekly_report(params: dict, end_day, name: str) -> tuple[str, str]:
    """Report on the 7 days before end_day (end_day itself not included), from MySQL."""
    from datetime import date, timedelta
    start = end_day - timedelta(days=7)
    prev = start - timedelta(days=7)
    con = mysql_connect(params)
    try:
        def q(sql, *p):
            with con.cursor() as cur:
                cur.execute(sql, p)
                return cur.fetchall()
        s0, e0, p0 = start.isoformat(), end_day.isoformat(), prev.isoformat()
        days = q("SELECT day, t_outdoor_avg, el_kwh, heat_hp_kwh, backup_heater_kwh, spf, "
                 "compressor_h, compressor_starts, defrosts, pressure_min, pressure_max, failures "
                 "FROM v_day WHERE day >= %s AND day < %s ORDER BY day", s0, e0)

        def totals(a, b):
            return q("SELECT SUM(calc_de_el_kwh), SUM(calc_dq_hp_kwh), SUM(calc_dq_buh_kwh), "
                     "SUM(CASE WHEN compressor = 1 THEN calc_dt_s END)/3600.0, AVG(t_outdoor), "
                     "MIN(t_outdoor), MAX(t_outdoor), MIN(water_pressure), MAX(water_pressure), "
                     "COUNT(*), SUM(1 - ok) FROM samples WHERE ts >= %s AND ts < %s", a, b)[0]
        el, heat, buh, comp_h, ta, ta_min, ta_max, pmin, pmax, n, failed = totals(s0, e0)
        el_prev, heat_prev, *_ = totals(p0, s0)
        starts = q("SELECT COUNT(*), AVG(duration_min), MIN(duration_min) FROM v_compressor_starts "
                   "WHERE start >= %s AND start < %s", s0, e0)[0]
        defr = q("SELECT COUNT(*), AVG(duration_min), MAX(duration_min) FROM v_defrosts "
                 "WHERE start >= %s AND start < %s", s0, e0)[0]
        bh = q("SELECT COUNT(*), SUM(duration_min), MAX(duration_min) FROM v_backup_heater "
               "WHERE start >= %s AND start < %s", s0, e0)[0]
        p_first = q("SELECT AVG(water_pressure) FROM samples WHERE ts >= %s AND ts < %s",
                    s0, (start + timedelta(days=1)).isoformat())[0][0]
        p_last = q("SELECT AVG(water_pressure) FROM samples WHERE ts >= %s AND ts < %s",
                   (end_day - timedelta(days=1)).isoformat(), e0)[0][0]
        faults = q("SELECT DISTINCT calc_fault_code FROM samples WHERE ts >= %s AND ts < %s "
                   "AND calc_fault_code IS NOT NULL AND calc_fault_code <> 'none'", s0, e0)
        try:
            sep = q("SELECT calc_sep_state, SUM(calc_dt_s)/3600.0, AVG(calc_sep_dlwt_k), "
                    "AVG(calc_sep_dret_k), AVG(flow), AVG(calc_sep_flow_sec_lpm) "
                    "FROM samples WHERE ts >= %s AND ts < %s AND calc_sep_state IS NOT NULL "
                    "GROUP BY calc_sep_state", s0, e0)
            hint = q("SELECT AVG(calc_sep_hint_dlwt_k), COUNT(calc_sep_hint_dlwt_k) "
                     "FROM samples WHERE ts >= %s AND ts < %s", s0, e0)[0]
        except Exception:  # noqa: BLE001 – older table without separator columns
            sep, hint = [], (None, 0)
    finally:
        con.close()

    el, heat, buh, el_prev, heat_prev = (None if x is None else float(x)
                                         for x in (el, heat, buh, el_prev, heat_prev))
    spf = heat / el if el and heat else None
    spf_prev = heat_prev / el_prev if el_prev and heat_prev else None
    wd = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    last_day = end_day - timedelta(days=1)
    subject = f"📊 {name}: weekly report {start:%Y-%m-%d} – {last_day:%Y-%m-%d}"
    L = [f"Period: {start:%Y-%m-%d} – {last_day:%Y-%m-%d}", ""]
    if not n:
        L += ["No data in this period."]
        return subject, "\n".join(L)
    L += ["ENERGY (calculated)",
          f"  Electricity HP (reg. 51): {_f(el, 1, ' kWh')}"
          + (f"   (previous week {_f(el_prev, 1, ' kWh')})" if el_prev else ""),
          f"  Heat HP:                  {_f(heat, 1, ' kWh')}",
          f"  SPF:                      {_f(spf, 2)}" + (f"   (previous week {_f(spf_prev, 2)})" if spf_prev else ""),
          f"  Backup heater:            {_f(buh, 1, ' kWh')}  ({bh[0]} runs, total {_f(bh[1], 0, ' min')}, "
          f"longest {_f(bh[2], 0, ' min')})",
          "",
          "OPERATION",
          f"  Compressor:               {_f(comp_h, 1, ' h')}, {starts[0]} starts "
          f"(avg {_f(starts[1], 0, ' min')}, shortest {_f(starts[2], 0, ' min')})",
          f"  Defrosts:                 {defr[0]} (avg {_f(defr[1], 1, ' min')}, longest {_f(defr[2], 1, ' min')})",
          f"  Outdoor temperature:      avg {_f(ta, 1, ' °C')} (min {_f(ta_min, 1)} / max {_f(ta_max, 1)})",
          f"  Water pressure:           {_f(pmin, 2)} – {_f(pmax, 2)} bar; daily mean first/last day "
          f"{_f(p_first, 2)} → {_f(p_last, 2)} bar",
          f"  Faults:                   {', '.join(r[0] for r in faults) if faults else 'none'}",
          f"  Data coverage:            {n} polls, {failed or 0} without connection",
          ""]
    L += separator_section(sep, hint)
    L += ["",
          "PER DAY  (Ta | electricity | heat | SPF | backup heater | starts | defrosts | pressure)"]
    for (day, t_a, e_kwh, h_kwh, b_kwh, d_spf, _ch, st, df, dmin, dmax, _fl) in days:
        d = day if isinstance(day, date) else datetime.fromisoformat(str(day)).date()
        L.append(f"  {wd[d.weekday()]} {d:%Y-%m-%d}  {_f(t_a, 1, '°')} | {_f(e_kwh, 1)} | {_f(h_kwh, 1)} | "
                 f"{_f(d_spf, 2)} | {_f(b_kwh, 1)} | {st} | {df} | {_f(dmin, 2)}–{_f(dmax, 2)}")
    L += ["", "Energy values are calculated from flow × ΔT and register 51 (see logger documentation).",
          "— Altherma 4 logger (read-only)"]
    return subject, "\n".join(L)


class WeeklyScheduler:
    """Sends the weekly report on the given weekday/hour. So that it is not sent
    again every minute or after every restart, the send date is stored in the
    MySQL table logger_state (k = 'last_weekly_report')."""

    def __init__(self, params: dict | None, notifier: Notifier, name: str, weekday: int, hour: int):
        self.params, self.n, self.name, self.weekday, self.hour = params, notifier, name, weekday, hour
        self.enabled = bool(params and notifier.channels and 1 <= weekday <= 7)
        self.last_check = 0.0

    def tick(self, now: float) -> None:
        if not self.enabled or now - self.last_check < 60:
            return
        self.last_check = now
        dt = datetime.fromtimestamp(now)
        if dt.isoweekday() != self.weekday or dt.hour < self.hour:
            return
        key = dt.date().isoformat()
        try:
            con = mysql_connect(self.params)
        except Exception as e:  # noqa: BLE001 – retry next minute
            log(f"weekly report: MySQL not reachable ({e}), retrying later")
            return
        try:
            with con.cursor() as cur:
                cur.execute("SELECT v FROM logger_state WHERE k = 'last_weekly_report'")
                row = cur.fetchone()
            if row and row[0] == key:
                return
            subject, text = weekly_report(self.params, dt.date(), self.name)
            self.n.notify(subject, text)
            with con.cursor() as cur:
                cur.execute("REPLACE INTO logger_state (k, v) VALUES ('last_weekly_report', %s)", (key,))
            con.commit()
        except Exception as e:  # noqa: BLE001
            log(f"weekly report failed: {e}")
        finally:
            con.close()


def build_notifier(args) -> Notifier:
    channels = []
    if args.telegram_token and args.telegram_chat_id:
        channels.append(TelegramChannel(args.telegram_token,
                                        [x.strip() for x in args.telegram_chat_id.split(",") if x.strip()],
                                        args.telegram_api_url))
    elif args.telegram_token or args.telegram_chat_id:
        log("Telegram: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID required – disabled")
    if args.smtp_host and args.mail_to:
        sec = args.smtp_security.lower()
        if sec not in ("starttls", "ssl", "none"):
            raise SystemExit("SMTP_SECURITY must be starttls, ssl or none")
        host, port = args.smtp_host, args.smtp_port
        if ":" in host and host.rsplit(":", 1)[1].isdigit():   # Grafana format host:port
            host, p = host.rsplit(":", 1)
            port = port or int(p)
            if not args.smtp_port and int(p) == 465 and sec == "starttls":
                sec = "ssl"
        port = port or {"ssl": 465, "starttls": 587, "none": 25}[sec]
        sender = args.mail_from or args.smtp_user
        if not sender:
            raise SystemExit("e-mail: MAIL_FROM or SMTP_USER required")
        channels.append(EmailChannel(host, port, args.smtp_user, args.smtp_password,
                                     sender, [x.strip() for x in args.mail_to.split(",") if x.strip()], sec))
    elif args.smtp_host or args.mail_to:
        log("e-mail: SMTP_HOST and MAIL_TO required – disabled")
    return Notifier(channels)


# ===========================================================================
# 8) Main – configuration via argument OR environment variable (Docker/.env)
# ===========================================================================

def env(name: str, default=None):
    v = os.environ.get(name)
    return default if v is None or v == "" else v


def env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None or v == "" else v.strip().lower() in ("1", "true", "yes", "on")


def touch_alive(path: str | None) -> None:
    """For the Docker health check: timestamp of the last successful poll."""
    if not path:
        return
    try:
        with open(path, "w") as f:
            f.write(str(time.time()))
    except OSError:
        pass


def build_sinks(args) -> list[BufferedSink]:
    sinks: list[BufferedSink] = []
    if args.mysql_host:
        if not (args.mysql_user and args.mysql_password and args.mysql_database):
            raise SystemExit("MySQL: MYSQL_USER, MYSQL_PASSWORD and MYSQL_DATABASE required")
        sinks.append(MySQLSink(args.mysql_host, args.mysql_port, args.mysql_user,
                               args.mysql_password, args.mysql_database, args.buffer))
        args.mysql_params = sinks[-1].params
    if args.influx_url:
        if not (args.influx_token and args.influx_org and args.influx_bucket):
            raise SystemExit("InfluxDB: INFLUXDB_TOKEN, INFLUXDB_ORG and INFLUXDB_BUCKET required")
        sinks.append(InfluxSink(args.influx_url, args.influx_token, args.influx_org,
                                args.influx_bucket, args.influx_measurement,
                                {"device": args.influx_device}, args.buffer))
    if not sinks:
        raise SystemExit("no output target enabled (configure MySQL and/or InfluxDB)")
    return sinks


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Daikin Altherma 4 Modbus TCP – read-only logger. "
                    "Every option can also be set via the environment variable in brackets.")
    ap.add_argument("host", nargs="?", default=env("ALTHERMA_HOST"),
                    help="IP address of the heat pump (ALTHERMA_HOST)")
    ap.add_argument("--port", type=int, default=int(env("ALTHERMA_PORT", 502)), help="(ALTHERMA_PORT)")
    ap.add_argument("--unit", type=int, default=int(env("ALTHERMA_UNIT_ID", 1)),
                    help="Modbus unit ID (ALTHERMA_UNIT_ID, default 1)")
    ap.add_argument("--interval", type=float, default=float(env("POLL_INTERVAL", 15)), metavar="SEC",
                    help="poll interval in s (POLL_INTERVAL, default 15)")
    ap.add_argument("--slow-interval", type=float, default=float(env("SLOW_INTERVAL", 300)),
                    metavar="SEC", help="read settings HR/CO every SEC s (SLOW_INTERVAL, default 300)")
    ap.add_argument("--max-gap", type=float, default=float(env("MAX_GAP", 120)), metavar="SEC",
                    help="do not integrate energy across larger gaps (MAX_GAP, default 120)")
    ap.add_argument("--buffer", type=int, default=int(env("BUFFER_ROWS", 20000)), metavar="N",
                    help="row buffer per target during DB outage (BUFFER_ROWS, default 20000 ≈ 3.5 days)")
    # targets
    ap.add_argument("--mysql-host", default=env("MYSQL_HOST"), help="(MYSQL_HOST; empty = off)")
    ap.add_argument("--mysql-port", type=int, default=int(env("MYSQL_PORT", 3306)), help="(MYSQL_PORT)")
    ap.add_argument("--mysql-user", default=env("MYSQL_USER"), help="(MYSQL_USER)")
    ap.add_argument("--mysql-password", default=env("MYSQL_PASSWORD"),
                    help="(MYSQL_PASSWORD – better via environment than as argument)")
    ap.add_argument("--mysql-database", default=env("MYSQL_DATABASE"), help="(MYSQL_DATABASE)")
    ap.add_argument("--influx-url", default=env("INFLUXDB_URL"),
                    help="e.g. http://influxdb:8086 (INFLUXDB_URL; empty = off)")
    ap.add_argument("--influx-token", default=env("INFLUXDB_TOKEN"), help="(INFLUXDB_TOKEN)")
    ap.add_argument("--influx-org", default=env("INFLUXDB_ORG"), help="(INFLUXDB_ORG)")
    ap.add_argument("--influx-bucket", default=env("INFLUXDB_BUCKET"), help="(INFLUXDB_BUCKET)")
    ap.add_argument("--influx-measurement", default=env("INFLUXDB_MEASUREMENT", "altherma4"),
                    help="(INFLUXDB_MEASUREMENT, default altherma4)")
    ap.add_argument("--influx-device", default=env("INFLUXDB_DEVICE_TAG", "altherma4"),
                    help="value of the tag 'device' (INFLUXDB_DEVICE_TAG)")
    # misc
    ap.add_argument("-v", "--verbose", action="store_true", default=env_bool("VERBOSE"),
                    help="print the full report on every poll (VERBOSE=1)")
    ap.add_argument("--quiet", action="store_true", default=env_bool("QUIET"),
                    help="no status line per poll, only errors (QUIET=1)")
    ap.add_argument("--alive-file", default=env("ALIVE_FILE", "/tmp/altherma4.alive"),
                    help="touched after every successful poll (ALIVE_FILE, Docker health check)")
    ap.add_argument("--once", action="store_true", help="single poll with full report, nothing is stored")
    ap.add_argument("--buh-kw-per-relay", type=float, default=env("BUH_KW_PER_RELAY") and float(env("BUH_KW_PER_RELAY")),
                    metavar="KW", help="optional: kW per backup heater relay (BUH_KW_PER_RELAY)")
    ap.add_argument("--flow-scale", type=float, default=float(env("FLOW_SCALE", 0.01)), metavar="F",
                    help="scaling of flow IR49 (FLOW_SCALE, default 0.01, verified on the unit)")
    # notifications
    ap.add_argument("--telegram-token", default=env("TELEGRAM_BOT_TOKEN"), help="(TELEGRAM_BOT_TOKEN)")
    ap.add_argument("--telegram-chat-id", default=env("TELEGRAM_CHAT_ID"),
                    help="one or more, comma-separated (TELEGRAM_CHAT_ID)")
    ap.add_argument("--telegram-api-url", default=env("TELEGRAM_API_URL", "https://api.telegram.org"),
                    help=argparse.SUPPRESS)
    ap.add_argument("--smtp-host", default=env("SMTP_HOST", env("SMTP_HOSTNAME", env("SMTP_SERVER"))),
                    help="(SMTP_HOST or SMTP_HOSTNAME/SMTP_SERVER, also host:port; empty = off)")
    ap.add_argument("--smtp-port", type=int, default=env("SMTP_PORT") and int(env("SMTP_PORT")),
                    help="(SMTP_PORT; default 587 for starttls, 465 for ssl)")
    ap.add_argument("--smtp-user", default=env("SMTP_USER", env("SMTP_USERNAME")), help="(SMTP_USER)")
    ap.add_argument("--smtp-password", default=env("SMTP_PASSWORD", env("SMTP_PASS")), help="(SMTP_PASSWORD)")
    ap.add_argument("--smtp-security", default=env("SMTP_SECURITY", "starttls"),
                    help="starttls | ssl | none (SMTP_SECURITY, default starttls)")
    ap.add_argument("--mail-from", default=env("MAIL_FROM", env("SMTP_FROM")),
                    help="sender (MAIL_FROM; default = SMTP_USER)")
    ap.add_argument("--mail-to", default=env("MAIL_TO", env("SMTP_TO")),
                    help="recipients, comma-separated (MAIL_TO)")
    ap.add_argument("--alert-name", default=env("ALERT_NAME", "Altherma 4"), help="name in the subject (ALERT_NAME)")
    ap.add_argument("--alert-confirm", type=int, default=int(env("ALERT_CONFIRM", 2)),
                    help="polls in a row before triggering/clearing (ALERT_CONFIRM, default 2)")
    ap.add_argument("--alert-repeat-hours", type=float, default=float(env("ALERT_REPEAT_HOURS", 12)),
                    help="reminder while a condition persists, 0 = off (ALERT_REPEAT_HOURS, default 12)")
    ap.add_argument("--alert-offline-min", type=float, default=float(env("ALERT_OFFLINE_MIN", 5)),
                    help="alarm if there is no connection for this long (ALERT_OFFLINE_MIN, default 5)")
    ap.add_argument("--alert-pressure-min", type=float,
                    default=env("ALERT_PRESSURE_MIN") and float(env("ALERT_PRESSURE_MIN")),
                    help="alarm below this water pressure in bar, empty = off (ALERT_PRESSURE_MIN)")
    ap.add_argument("--alert-warnings", action=argparse.BooleanOptionalAction,
                    default=env_bool("ALERT_WARNINGS", True),
                    help="also report warnings (IR21=2) (ALERT_WARNINGS, default on)")
    def fenv(name, default=None):
        v = env(name)
        return float(v) if v is not None else default
    ap.add_argument("--alert-pressure-drop", type=float, default=fenv("ALERT_PRESSURE_DROP", 0.2),
                    help="alarm on a pressure drop ≥ X bar within the window, 0 = off (ALERT_PRESSURE_DROP, default 0.2)")
    ap.add_argument("--alert-pressure-drop-hours", type=float, default=fenv("ALERT_PRESSURE_DROP_HOURS", 24),
                    help="window for the pressure drop in h (ALERT_PRESSURE_DROP_HOURS, default 24)")
    ap.add_argument("--alert-pressure-pump-tol", type=float, default=fenv("ALERT_PRESSURE_PUMP_TOL", 5),
                    help="pressure drop: only compare readings within ± X %% pump speed "
                         "(ALERT_PRESSURE_PUMP_TOL, default 5)")
    ap.add_argument("--alert-buh-min", type=float, default=fenv("ALERT_BUH_MIN", 10),
                    help="alarm if the backup heater runs longer than X min, 0 = off (ALERT_BUH_MIN, default 10)")
    ap.add_argument("--alert-buh-kwh-day", type=float, default=fenv("ALERT_BUH_KWH_DAY", 2),
                    help="message if the backup heater uses ≥ X kWh in a day, 0 = off (ALERT_BUH_KWH_DAY, default 2)")
    ap.add_argument("--alert-starts-per-hour", type=float, default=fenv("ALERT_STARTS_PER_HOUR", 3),
                    help="alarm on more compressor starts per hour, 0 = off (ALERT_STARTS_PER_HOUR, default 3)")
    ap.add_argument("--alert-lwt-dev", type=float, default=fenv("ALERT_LWT_DEV", 5),
                    help="alarm if leaving water is X K below target, 0 = off (ALERT_LWT_DEV, default 5)")
    ap.add_argument("--alert-lwt-min", type=float, default=fenv("ALERT_LWT_MIN", 30),
                    help="… for longer than X min (ALERT_LWT_MIN, default 30)")
    ap.add_argument("--alert-flow-min", type=float, default=fenv("ALERT_FLOW_MIN", 3),
                    help="alarm on flow below X l/min with the pump running (> 2 min), 0 = off "
                         "(ALERT_FLOW_MIN, default 3)")
    ap.add_argument("--alert-defrost-max-min", type=float, default=fenv("ALERT_DEFROST_MAX_MIN", 15),
                    help="alarm if a defrost lasts longer than X min, 0 = off (ALERT_DEFROST_MAX_MIN)")
    ap.add_argument("--alert-defrost-per-hour", type=float, default=fenv("ALERT_DEFROST_PER_HOUR", 2),
                    help="alarm on more defrosts per hour, 0 = off (ALERT_DEFROST_PER_HOUR, default 2)")
    ap.add_argument("--alert-frost", action=argparse.BooleanOptionalAction,
                    default=env_bool("ALERT_FROST", False),
                    help="report frost protection, but only while leaving/return water is below "
                         "--alert-frost-water-min (ALERT_FROST, default off)")
    ap.add_argument("--alert-frost-water-min", type=float, default=fenv("ALERT_FROST_WATER_MIN", 10.0),
                    help="water temperature in °C for the frost alarm (ALERT_FROST_WATER_MIN, default 10)")
    ap.add_argument("--alert-db-min", type=float, default=fenv("ALERT_DB_MIN", 30),
                    help="alarm if a database has not written for X min (ALERT_DB_MIN, default 30)")
    # hydraulic separator (secondary sensors)
    ap.add_argument("--sec-supply-source", default=env("SEC_SUPPLY_SOURCE"),
                    help="secondary supply: w1:<id> | file:<path> | modbus://host[:port]/unit/ir|hr/addr[/scale] "
                         "(SEC_SUPPLY_SOURCE; empty = off)")
    ap.add_argument("--sec-return-source", default=env("SEC_RETURN_SOURCE"),
                    help="secondary return, same format (SEC_RETURN_SOURCE; empty = off)")
    ap.add_argument("--sec-supply-offset", type=float, default=fenv("SEC_SUPPLY_OFFSET", 0.0),
                    help="correction in K for the secondary supply (SEC_SUPPLY_OFFSET)")
    ap.add_argument("--sec-return-offset", type=float, default=fenv("SEC_RETURN_OFFSET", 0.0),
                    help="correction in K for the secondary return (SEC_RETURN_OFFSET)")
    ap.add_argument("--w1-base", default=env("W1_BASE", "/sys/bus/w1/devices"), help=argparse.SUPPRESS)
    ap.add_argument("--hyd-tol", type=float, default=fenv("HYD_TOL", 1.0),
                    help="tolerance in K for 'equal' (HYD_TOL, default 1.0)")
    ap.add_argument("--hyd-avg-min", type=float, default=fenv("HYD_AVG_MIN", 5),
                    help="averaging window in min (HYD_AVG_MIN, default 5)")
    ap.add_argument("--hyd-settle-min", type=float, default=fenv("HYD_SETTLE_MIN", 10),
                    help="evaluate only after X min of stable operation (HYD_SETTLE_MIN, default 10)")
    ap.add_argument("--alert-hyd-min", type=float, default=fenv("ALERT_HYD_MIN", 60),
                    help="message if a separator imbalance persists for X min, 0 = off (ALERT_HYD_MIN, default 60)")
    ap.add_argument("--alert-hyd-clear-min", type=float, default=fenv("ALERT_HYD_CLEAR_MIN", 30),
                    help="clear after X min without imbalance (ALERT_HYD_CLEAR_MIN, default 30)")
    ap.add_argument("--heartbeat-url", default=env("HEARTBEAT_URL"),
                    help="dead man's switch, e.g. https://hc-ping.com/<uuid> (HEARTBEAT_URL; empty = off)")
    ap.add_argument("--heartbeat-min", type=float, default=fenv("HEARTBEAT_MIN", 5),
                    help="ping interval in min (HEARTBEAT_MIN, default 5)")
    ap.add_argument("--report-weekday", type=int, default=int(env("REPORT_WEEKDAY", 1)),
                    help="weekly report: 1 = Monday … 7 = Sunday, 0 = off (REPORT_WEEKDAY, default 1)")
    ap.add_argument("--report-hour", type=int, default=int(env("REPORT_HOUR", 7)),
                    help="weekly report from this hour on (REPORT_HOUR, default 7)")
    ap.add_argument("--report-now", action="store_true",
                    help="print/send the weekly report of the last 7 days now and exit (needs MySQL)")
    ap.add_argument("--test-notify", action="store_true",
                    help="send a test message over all channels and exit")
    ap.add_argument("--scan", type=int, metavar="MAXADDR",
                    help="only raw scan of all register types 1..MAXADDR (e.g. 300)")
    args = ap.parse_args()
    if not args.host and not (args.test_notify or args.report_now):
        ap.error("IP address of the heat pump missing (argument or ALTHERMA_HOST)")
    global FLOW_SCALE
    FLOW_SCALE = args.flow_scale

    if args.report_now:
        if not (args.mysql_host and args.mysql_user and args.mysql_database):
            print("weekly report needs MySQL (MYSQL_HOST/USER/PASSWORD/DATABASE).", file=sys.stderr)
            return 1
        from datetime import timedelta
        params = dict(host=args.mysql_host, port=args.mysql_port, user=args.mysql_user,
                      password=args.mysql_password, database=args.mysql_database,
                      connect_timeout=5, charset="utf8mb4")
        subject, text = weekly_report(params, datetime.now().date() + timedelta(days=1), args.alert_name)
        print(subject + "\n\n" + text)
        n = build_notifier(args)
        if n.channels:
            n.notify(subject, text)
            n.close(60)
        return 0

    if args.test_notify:
        n = build_notifier(args)
        if not n.channels:
            print("no notification channel configured (Telegram or e-mail).", file=sys.stderr)
            return 1
        for ch in n.channels:
            try:
                ch.send(f"✅ {args.alert_name}: test message",
                        f"Test message from the Altherma 4 logger ({datetime.now():%Y-%m-%d %H:%M}).\n"
                        f"Channel: {ch.name}. If you can read this, notifications work.")
                print(f"{ch.name}: sent")
            except Exception as e:  # noqa: BLE001
                print(f"{ch.name}: ERROR – {e}", file=sys.stderr)
                return 1
        return 0

    if args.scan:
        with ReadOnlyModbusTCP(args.host, args.port, args.unit) as cli:
            scan(cli, args.scan)
        return 0

    try:
        ext = ExternalTemps({
            "ext_t_sec_supply": ExtSource(args.sec_supply_source, args.sec_supply_offset, args.w1_base)
                            if args.sec_supply_source else None,
            "ext_t_sec_return": ExtSource(args.sec_return_source, args.sec_return_offset, args.w1_base)
                            if args.sec_return_source else None})
    except ValueError as e:
        ap.error(str(e))
    hyd = HydraulicAnalyzer(args.hyd_tol, args.hyd_avg_min, args.hyd_settle_min, args.alert_flow_min)
    calc = Calculator(args.buh_kw_per_relay, args.max_gap, hyd)

    if args.once:
        try:
            with ReadOnlyModbusTCP(args.host, args.port, args.unit) as cli:
                m, raw = poll(cli, slow=True)
        except (OSError, ModbusError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        m.update(ext.read())
        now = time.time()
        print_report(datetime.fromtimestamp(now).isoformat(timespec="seconds"),
                     m, calc.compute(m, now), raw)
        return 0

    sinks = build_sinks(args)
    notifier = build_notifier(args)
    monitor = AlertMonitor(notifier, args)
    heartbeat = Heartbeat(args.heartbeat_url, args.heartbeat_min)
    if args.report_weekday and notifier.channels and not getattr(args, "mysql_params", None):
        log("weekly report disabled: needs MySQL")
    weekly = WeeklyScheduler(getattr(args, "mysql_params", None), notifier, args.alert_name,
                             args.report_weekday, args.report_hour)
    stop = {"flag": False}

    def _stop(*_):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, _stop)  # docker stop -> clean shutdown

    print(f"Logging {args.host}:{args.port} every {args.interval:g} s (settings every "
          f"{args.slow_interval:g} s) to: {', '.join(s.name for s in sinks)} – read-only"
          + (f"; notifications: {', '.join(ch.name for ch in notifier.channels)}" if notifier.channels else "")
          + (f"; secondary sensors: {', '.join(s.spec for s in ext.sources.values())}" if ext.active else ""),
          flush=True)
    next_t = time.monotonic()
    last_slow = None
    consec_fail = 0
    try:
        while not stop["flag"]:
            now = time.time()
            ts = datetime.fromtimestamp(now).isoformat(timespec="seconds")
            slow = (last_slow is None or args.slow_interval <= 0
                    or time.monotonic() - last_slow >= args.slow_interval)
            t0 = time.monotonic()
            cli = ReadOnlyModbusTCP(args.host, args.port, args.unit)
            diag = {"diag_consec_fail": consec_fail, "diag_slow": int(slow)}
            try:
                with cli:
                    m, raw = poll(cli, slow)
                m.update(ext.read())
                diag.update(diag_poll_ms=round((time.monotonic() - t0) * 1000),
                            diag_requests=cli.n_requests, diag_exceptions=cli.n_exceptions,
                            diag_error_kind=None)
                if slow:
                    last_slow = time.monotonic()
                c = calc.compute(m, now)
                values = {**m, **{f"calc_{k}": v for k, v in c.items()}, **diag,
                          "raw_json": json.dumps(raw, separators=(",", ":"))}
                row = Row(ts, now, True, None, values)
                consec_fail = 0
                touch_alive(args.alive_file)
                if args.verbose:
                    print_report(ts, m, c, raw)
                elif not args.quiet:
                    print(status_line(ts, m, c, diag), flush=True)
            except (OSError, ModbusError) as e:
                consec_fail += 1
                diag.update(diag_poll_ms=round((time.monotonic() - t0) * 1000),
                            diag_requests=cli.n_requests, diag_exceptions=cli.n_exceptions,
                            diag_slow=0, diag_error_kind=error_kind(e))
                row = Row(ts, now, False, str(e), diag)
                m = c = None
                log(f"Modbus error ({error_kind(e)}, {consec_fail}× in a row): {e}")

            for s in sinks:
                s.write(row)
            try:
                monitor.update(row.ok, m, c, now, sinks)
            except Exception as e:  # noqa: BLE001 – alarm logic must never stop the logger
                log(f"alarm check failed: {e!r}")
            heartbeat.tick(now)
            weekly.tick(now)

            # fixed grid without drift; re-anchor after a long outage
            next_t += args.interval
            delay = next_t - time.monotonic()
            if delay < 0:
                next_t = time.monotonic()
                delay = 0
            end = time.monotonic() + delay
            while not stop["flag"] and time.monotonic() < end:
                time.sleep(min(0.5, end - time.monotonic()))
    except KeyboardInterrupt:
        pass
    finally:
        for s in sinks:
            if s.queue:
                log(f"{s.name}: {len(s.queue)} rows not written on shutdown")
            s.close()
        notifier.close()
        print("Stopped.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
