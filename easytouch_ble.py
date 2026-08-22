#!/usr/bin/env python3
# Copyright 2026 Clint Goudie-Nice
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""
BLE protocol module for Micro-Air EasyTouch RV thermostats.

EasyTouch RV controllers expose a single vendor GATT service and speak a
line-oriented JSON protocol: the client writes a JSON command object to the
command characteristic and reads a JSON response object from the return
characteristic.  See PROTOCOL.md for the full specification.

  - Vendor service UUID:  000000FF-0000-1000-8000-00805F9B34FB
  - Command  (EE01): host -> unit, JSON command (write)
  - Return   (FF01): unit -> host, JSON response (read / notify)
  - Password (DD01): passcode handshake (write + read)

Messages are plain UTF-8 JSON objects with a top-level ``Type`` field.  There
is no binary framing, checksum, or manual chunking; the ATT layer performs
long reads/writes for payloads larger than the negotiated MTU.

Connection model: transient sessions (connect, exchange, disconnect).
Adapter selection, cross-process claims coordination, and connection-parameter
tuning come from bleak-connection-manager v2's process-wide bleak catcher:
the entry point must call ``install_bleak_catcher(...)`` BEFORE importing
this module, because the ``BleakClient`` / ``BleakScanner`` bindings below
are captured at import time.  Retry semantics come from
bleak-retry-connector's ``establish_connection``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field

from bleak import BleakClient, BleakScanner
from bleak_retry_connector import establish_connection
from bleak_connection_manager.validators import (
    tolerate_late_gatt,
    validate_char_exists,
)

logger = logging.getLogger(__name__)

# ── Vendor GATT UUIDs ─────────────────────────────────────────────────────────

SERVICE_UUID  = "000000ff-0000-1000-8000-00805f9b34fb"
CMD_UUID      = "0000ee01-0000-1000-8000-00805f9b34fb"   # write: JSON command
RETURN_UUID   = "0000ff01-0000-1000-8000-00805f9b34fb"   # read/notify: JSON response
PASSWORD_UUID = "0000dd01-0000-1000-8000-00805f9b34fb"   # passcode handshake

# BLE advertised name substring for EasyTouch RV controllers
NAME_SUBSTRING = "EasyTouch"

# ── Mode enumeration ──────────────────────────────────────────────────────────

MODE_OFF          = 0
MODE_FAN_ONLY     = 1
MODE_COOL         = 2
MODE_GAS_HEAT     = 3
MODE_FURNACE      = 4
MODE_HEAT_PUMP    = 5
MODE_DRY          = 6
MODE_HEAT_STRIP   = 7
MODE_AUTO         = 8
MODE_AUTO_HS      = 9
MODE_AUTO_HP      = 10
MODE_AUTO_FURNACE = 11
MODE_ELECTRIC     = 12
MODE_ZONE_OFF     = 16

MODE_NAMES = {
    MODE_OFF: "Off",
    MODE_FAN_ONLY: "Fan Only",
    MODE_COOL: "Cool",
    MODE_GAS_HEAT: "Gas Heat",
    MODE_FURNACE: "Furnace",
    MODE_HEAT_PUMP: "Heat Pump",
    MODE_DRY: "Dry",
    MODE_HEAT_STRIP: "Heat Strip",
    MODE_AUTO: "Auto",
    MODE_AUTO_HS: "Auto Heat Strip",
    MODE_AUTO_HP: "Auto Heat Pump",
    MODE_AUTO_FURNACE: "Auto Furnace",
    MODE_ELECTRIC: "Electric Heat",
    MODE_ZONE_OFF: "Zone Off",
}

HEAT_MODES = {MODE_GAS_HEAT, MODE_FURNACE, MODE_HEAT_PUMP, MODE_HEAT_STRIP, MODE_ELECTRIC}
AUTO_MODES = {MODE_AUTO, MODE_AUTO_HS, MODE_AUTO_HP, MODE_AUTO_FURNACE}

# ── Fault codes ───────────────────────────────────────────────────────────────

FAULT_NAMES = {
    0: "",
    1: "NO COMMUNICATION",
    2: "BAD REMOTE SENSOR",
    3: "BAD OUTSIDE SENSOR",
    4: "BAD FREEZE SENSOR",
    5: "FREEZE DETECTED",
    6: "BAD HUMIDITY SENSOR",
    7: "NO AC POWER",
    8: "INVALID CONFIG",
    9: "LOW DC VOLTAGE",
    10: "BAD INDOOR SENSOR",
    11: "INSIDE COIL SENSOR",
    12: "OUTSIDE COIL SENSOR",
    13: "LOW REFRIGERANT",
}


def fault_name(code: int) -> str:
    """Return the human-readable fault string for a fault code."""
    if code <= 0:
        return ""
    return FAULT_NAMES.get(code, "UNDEFINED FAULT")


# ── Status / config data classes ──────────────────────────────────────────────

# Z_sts per-zone array indices (see PROTOCOL.md)
_IDX_AUTOHEAT_SP = 0
_IDX_AUTOCOOL_SP = 1
_IDX_COOL_SP     = 2
_IDX_HEAT_SP     = 3
_IDX_DRY_SP      = 4
_IDX_RH_SP       = 5
_IDX_FAN_FANONLY = 6
_IDX_FAN_COOL    = 7
_IDX_FAN_ELEC    = 8
_IDX_FAN_AUTO    = 9
_IDX_MODE        = 10
_IDX_FAN_GAS     = 11
_IDX_INSIDE_TEMP = 12
_IDX_OUTSIDE     = 13
_IDX_FAULT       = 14
_IDX_ACTIVITY    = 15

# Activity bitfield (index 15)
ACT_CYCLE   = 0x01
ACT_COOLING = 0x02
ACT_HEATING = 0x04

# ── Fan encoding ──────────────────────────────────────────────────────────────
# Fan *values* (status fan slots and *Fan change keys):
#   0x80 = full auto, 0x40|n = auto within manual range, low nibble = speed,
#   0x00 = off.  Values are masked with 0xCF before writing.
# FA *capability* entries (config FA[mode]) carry allowed options in the upper
# bits and the max discrete speed in the low nibble.

FAN_FULL_AUTO  = 0x80
FAN_RANGE_AUTO = 0x40
FAN_SPEED_MASK = 0x0F
FAN_WRITE_MASK = 0xCF

FA_ALLOW_FULL_AUTO  = 0x80
FA_ALLOW_RANGE_AUTO = 0x40
FA_ALLOW_OFF        = 0x20
FA_FIXED_SPEED      = 0x10

# Which Change key carries the fan value for a given mode.
_FAN_CHANGE_KEYS = {
    MODE_FAN_ONLY: "fanOnly",
    MODE_COOL: "coolFan",
    MODE_FURNACE: "gasFan",
    MODE_GAS_HEAT: "eleFan",
    MODE_HEAT_PUMP: "eleFan",
    MODE_HEAT_STRIP: "eleFan",
    MODE_ELECTRIC: "eleFan",
    MODE_AUTO: "autoFan",
    MODE_AUTO_HS: "autoFan",
    MODE_AUTO_HP: "autoFan",
    MODE_AUTO_FURNACE: "autoFan",
}


def fan_change_key(mode: int) -> str | None:
    """The Change key that sets the fan for ``mode`` (None if no fan control)."""
    return _FAN_CHANGE_KEYS.get(mode)


_SPEED_LABELS = {
    1: ["On"],
    2: ["Low", "High"],
    3: ["Low", "Medium", "High"],
}


def fan_options(fa_entry: int) -> list[tuple[str, int]]:
    """Build (label, fan-value) choices for one mode's FA capability entry.

    Three families exist, and a unit may allow several:
      0x80          full auto -- the unit picks the speed
      0x40 | speed  cycled    -- runs at that speed, on and off with the
                                compressor ("Cycled and auto fan selections
                                turn on and off with the compressor or heat
                                pump" -- EasyTouch manual)
      speed         manual    -- runs continuously at that speed

    Cycled entries are offered for *every* speed.  Units that advertise no
    full auto (common on GE) would otherwise get a single "Auto" that
    silently means cycled-at-lowest-speed -- the reduced airflow that ices
    an evaporator coil.  Returns an empty list for a fixed-speed fan.
    """
    if fa_entry & FA_FIXED_SPEED:
        return []
    max_speed = fa_entry & FAN_SPEED_MASK
    labels = _SPEED_LABELS.get(max_speed) or [
        "Speed %d" % i for i in range(1, max_speed + 1)
    ]

    options: list[tuple[str, int]] = []
    if fa_entry & FA_ALLOW_FULL_AUTO:
        options.append(("Auto", FAN_FULL_AUTO))
    if fa_entry & FA_ALLOW_RANGE_AUTO:
        for speed, label in enumerate(labels, start=1):
            options.append(("Auto %s" % label, FAN_RANGE_AUTO | speed))
    for speed, label in enumerate(labels, start=1):
        options.append((label, speed))
    if fa_entry & FA_ALLOW_OFF:
        options.append(("Off", 0))
    return options


def fan_option_index(options: list[tuple[str, int]], value: int) -> int | None:
    """Map a raw fan value from a status slot to its index in ``options``.

    Range-auto values collapse onto their manual speed.
    """
    # Exact match first, so cycled-at-speed lands on its own entry.
    for i, (_label, v) in enumerate(options):
        if v == value:
            return i
    # Otherwise fall back to the same family at any speed.
    if value & FAN_FULL_AUTO:
        for i, (_label, v) in enumerate(options):
            if v & FAN_FULL_AUTO:
                return i
    elif value & FAN_RANGE_AUTO:
        for i, (_label, v) in enumerate(options):
            if v & FAN_RANGE_AUTO:
                return i
    target = value & FAN_SPEED_MASK
    for i, (_label, v) in enumerate(options):
        if v == target:
            return i
    return None

# Outside-air-temperature sentinels: outside these bounds there is no sensor.
_OUTSIDE_MIN = -60
_OUTSIDE_MAX = 160


@dataclass
class ZoneStatus:
    """Decoded status for a single thermostat zone."""
    zone: int
    mode: int
    inside_temp: int | None
    outside_temp: int | None
    cool_sp: int
    heat_sp: int
    dry_sp: int
    auto_cool_sp: int
    auto_heat_sp: int
    fault: int
    activity: int
    system_power: bool
    fan_fanonly: int = 0
    fan_cool: int = 0
    fan_elec: int = 0
    fan_auto: int = 0
    fan_gas: int = 0
    rev: str = ""
    weather_temp: int | None = None      # PRM[2], cloud weather (wifi units)
    faceplate_temp: int | None = None    # PRM[3], faceplate sensor

    def fan_value(self, mode: int | None = None) -> int:
        """The raw fan value for ``mode`` (defaults to the current mode)."""
        m = self.mode if mode is None else mode
        if m == MODE_FAN_ONLY:
            return self.fan_fanonly
        if m == MODE_COOL:
            return self.fan_cool
        if m == MODE_FURNACE:
            return self.fan_gas
        if m in AUTO_MODES:
            return self.fan_auto
        if m in HEAT_MODES:
            return self.fan_elec
        return 0

    @property
    def mode_name(self) -> str:
        return MODE_NAMES.get(self.mode, "Unknown")

    @property
    def fault_text(self) -> str:
        return fault_name(self.fault)

    @property
    def is_cooling(self) -> bool:
        return bool(self.activity & ACT_COOLING)

    @property
    def is_heating(self) -> bool:
        return bool(self.activity & ACT_HEATING)

    @property
    def running_state(self) -> str:
        if self.is_cooling:
            return "cooling"
        if self.is_heating:
            return "heating"
        return "idle"

    def active_setpoint(self) -> int | None:
        """The setpoint relevant to the current mode (None for off/fan)."""
        if self.mode == MODE_COOL:
            return self.cool_sp
        if self.mode == MODE_DRY:
            return self.dry_sp
        if self.mode in HEAT_MODES:
            return self.heat_sp
        return None


@dataclass
class ZoneConfig:
    """Decoded capabilities for a single zone (from Get Config)."""
    zone: int
    available_modes: int = 0
    min_cool: int = 65
    max_cool: int = 85
    min_heat: int = 45
    max_heat: int = 85
    fan_array: list[int] = field(default_factory=list)

    def mode_available(self, mode: int) -> bool:
        return bool(self.available_modes & (1 << mode))


def _valid_temp(value: int | None) -> int | None:
    """Clamp a raw °F reading to None when it is a no-sensor sentinel."""
    if value is None:
        return None
    if value == 255 or value <= _OUTSIDE_MIN or value >= _OUTSIDE_MAX:
        return None
    return value


def _parse_status(obj: dict) -> dict[int, ZoneStatus]:
    """Decode a Status response into a {zone: ZoneStatus} map."""
    prm = obj.get("PRM") or [0, 0, 0, 0]
    system_power = bool(int(prm[1]) & 0x08) if len(prm) > 1 else True
    weather = _valid_temp(int(prm[2])) if len(prm) > 2 else None
    faceplate = _valid_temp(int(prm[3])) if len(prm) > 3 else None

    result: dict[int, ZoneStatus] = {}
    z_sts = obj.get("Z_sts") or {}
    for zone_key, arr in z_sts.items():
        try:
            zone = int(zone_key)
        except (TypeError, ValueError):
            continue
        if not isinstance(arr, list) or len(arr) < 16:
            logger.warning("Short Z_sts array for zone %s: %r", zone_key, arr)
            continue

        outside = int(arr[_IDX_OUTSIDE])
        if outside <= _OUTSIDE_MIN or outside >= _OUTSIDE_MAX:
            outside = None

        result[zone] = ZoneStatus(
            zone=zone,
            mode=int(arr[_IDX_MODE]),
            inside_temp=int(arr[_IDX_INSIDE_TEMP]),
            outside_temp=outside,
            cool_sp=int(arr[_IDX_COOL_SP]),
            heat_sp=int(arr[_IDX_HEAT_SP]),
            dry_sp=int(arr[_IDX_DRY_SP]),
            auto_cool_sp=int(arr[_IDX_AUTOCOOL_SP]),
            auto_heat_sp=int(arr[_IDX_AUTOHEAT_SP]),
            fault=int(arr[_IDX_FAULT]),
            activity=int(arr[_IDX_ACTIVITY]),
            system_power=system_power,
            fan_fanonly=int(arr[_IDX_FAN_FANONLY]),
            fan_cool=int(arr[_IDX_FAN_COOL]),
            fan_elec=int(arr[_IDX_FAN_ELEC]),
            fan_auto=int(arr[_IDX_FAN_AUTO]),
            fan_gas=int(arr[_IDX_FAN_GAS]),
            rev=str(obj.get("REV") or ""),
            weather_temp=weather,
            faceplate_temp=faceplate,
        )
    return result


def _parse_config(obj: dict) -> ZoneConfig | None:
    """Decode a Config response (its CFG string) into a ZoneConfig."""
    cfg_raw = obj.get("CFG")
    if cfg_raw is None:
        return None
    try:
        cfg = json.loads(cfg_raw) if isinstance(cfg_raw, str) else cfg_raw
    except json.JSONDecodeError:
        logger.warning("Unparseable CFG payload: %r", cfg_raw)
        return None

    spl = cfg.get("SPL") or []
    zc = ZoneConfig(zone=int(cfg.get("Zone", 0)))
    zc.available_modes = int(cfg.get("MAV", 0))
    if len(spl) >= 4:
        zc.min_cool, zc.max_cool, zc.min_heat, zc.max_heat = (
            int(spl[0]), int(spl[1]), int(spl[2]), int(spl[3])
        )
    zc.fan_array = [int(x) for x in (cfg.get("FA") or [])]
    return zc


# ── ExtraData diagnostics (per-A/C-model) ─────────────────────────────────────

@dataclass
class ExtraData:
    """Decoded ExtraData diagnostics.

    Field availability depends on the A/C platform (serial prefix); missing
    or invalid-sentinel values are None.  ``flags`` holds decoded state bits
    with normalised names; ``raw`` keeps the whole response for debugging.
    """
    zone: int | None = None
    outside_air: int | None = None     # °F (OAT)
    inside_coil: int | None = None     # °F (InCoil / FS on 351)
    outside_coil: int | None = None    # °F (OutCoil)
    faceplate: int | None = None       # °F (fPlate, Atwood)
    room_sensor: int | None = None     # °F (RMSen, Atwood)
    vdc: float | None = None           # supply voltage (GE)
    flags: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)


def _temp_or_none(obj: dict, key: str, zero_invalid: bool = False,
                  atwood: bool = False) -> int | None:
    """Read a °F field honouring the model's invalid-value sentinels."""
    if key not in obj:
        return None
    try:
        v = int(obj[key])
    except (TypeError, ValueError):
        return None
    if atwood:
        return None if v >= 200 else v
    if zero_invalid and v == 0:
        return None
    return _valid_temp(v)


def parse_extra_data(obj: dict, model_prefix: str) -> ExtraData:
    """Decode an ExtraData response for the given 3-digit model prefix.

    Unknown prefixes return an ExtraData with only ``zone`` and ``raw`` set.
    Note the internal/remote sensor bit is *not* consistent across models.
    """
    ed = ExtraData(raw=obj)
    if "Zone" in obj:
        try:
            ed.zone = int(obj["Zone"])
        except (TypeError, ValueError):
            pass
    ss = int(obj.get("SS", 0) or 0)
    os_bits = int(obj.get("OS", 0) or 0)
    f = ed.flags

    if model_prefix == "347":            # GE
        ed.outside_air = _temp_or_none(obj, "OAT")
        ed.inside_coil = _temp_or_none(obj, "InCoil")
        ed.outside_coil = _temp_or_none(obj, "OutCoil")
        if "VDC" in obj:
            vdc = int(obj["VDC"]) / 100.0
            ed.vdc = vdc if 8.0 <= vdc <= 20.0 else None
        f["remote_sensor"] = bool(ss & 0x02)

    elif model_prefix == "350":          # Dometic CCC2
        ed.outside_air = _temp_or_none(obj, "OAT", zero_invalid=True)
        f["defrost"] = bool(ss & 0x01)
        f["remote_sensor"] = bool(ss & 0x02)
        f["eh_override_available"] = bool(ss & 0x04)
        f["eh_override"] = bool(ss & 0x08)
        f["eh_lockout"] = bool(ss & 0x10)
        f["cycle_delay"] = bool(int(obj.get("Output", 0) or 0) & 0x20)

    elif model_prefix == "351":          # Dometic Single-Zone
        ed.inside_coil = _temp_or_none(obj, "FS", zero_invalid=True)
        ed.outside_air = _temp_or_none(obj, "OAT", zero_invalid=True)
        f["remote_sensor"] = bool(ss & 0x200)
        f["gas_override_enabled"] = bool(int(obj.get("GCE", 0) or 0) & 0x01)
        f["gas_override"] = bool(ss & 0x01)
        f["gas_lockout"] = bool(ss & 0x02)
        f["defrost"] = bool(ss & 0x02)

    elif model_prefix in ("352", "359"):  # Dometic
        f["active"] = bool(os_bits & 0x01)
        f["cooling"] = bool(os_bits & 0x02)
        f["heating"] = bool(os_bits & 0x04)
        f["gas_assist"] = bool(os_bits & 0x08)
        f["eh_lockout"] = bool(os_bits & 0x10)
        f["start_delay"] = bool(os_bits & 0x40)
        f["remote_sensor"] = not bool(ss & 0x10)
        f["gas_assist_enabled"] = bool(ss & 0x20)

    elif model_prefix in ("354", "355"):  # Coleman 8000/9000
        f["remote_sensor"] = not bool(ss & 0x200)
        f["gas_relay"] = bool(ss & 0x10)
        f["gas_lockout"] = bool(ss & 0x08)
        f["gas_assist_enabled"] = bool(ss & 0x04)
        f["gas_assist"] = bool(ss & 0x04) and bool(ss & 0x10)
        f["high_heat_available"] = bool(ss & 0x01)
        f["high_heat"] = bool(ss & 0x01) and bool(ss & 0x80)

    elif model_prefix == "356":          # Coleman two-stage
        f["remote_sensor"] = not bool(ss & 0x10)
        f["eh_active"] = bool(ss & 0x01)
        f["eh_lockout"] = bool(ss & 0x02)
        f["eh_disabled"] = bool(ss & 0x04)
        f["active"] = bool(os_bits & 0x01)
        f["cooling"] = bool(os_bits & 0x02)
        f["heating"] = bool(os_bits & 0x04)
        f["start_delay"] = bool(os_bits & 0x40)

    elif model_prefix == "358":          # Atwood
        f["remote_sensor"] = not bool(int(obj.get("useFPS", 0) or 0))
        ed.faceplate = _temp_or_none(obj, "fPlate", atwood=True)
        ed.room_sensor = _temp_or_none(obj, "RMSen", atwood=True)
        ed.inside_coil = _temp_or_none(obj, "InCoil", atwood=True)
        ed.outside_coil = _temp_or_none(obj, "OutCoil", atwood=True)
        ed.outside_air = _temp_or_none(obj, "OAT", atwood=True)

    return ed


def model_prefix_from_name(name: str) -> str:
    """Extract the 3-digit A/C model prefix from a BLE advertised name."""
    digits = "".join(c for c in (name or "") if c.isdigit())
    serial = digits[-9:] if len(digits) >= 9 else digits
    return serial[:3] if len(serial) >= 3 else ""


# ── Discovery ─────────────────────────────────────────────────────────────────

@dataclass
class DiscoveredThermostat:
    """An EasyTouch RV controller found during BLE scanning."""
    mac: str
    name: str
    rssi: int = -100

    @property
    def serial(self) -> str:
        """The 9-digit serial (trailing digits of the advertised name)."""
        digits = "".join(c for c in self.name if c.isdigit())
        return digits[-9:] if len(digits) >= 9 else digits

    @property
    def model_prefix(self) -> str:
        """The 3-digit A/C model prefix (first 3 digits of the serial)."""
        s = self.serial
        return s[:3] if len(s) >= 3 else ""


async def scan_for_thermostats(
    timeout: float = 12.0,
) -> list[DiscoveredThermostat]:
    """Scan BLE for EasyTouch RV controllers.

    With the bleak catcher installed (wrap_scanner=True), the scan runs on a
    claim-coordinated adapter chosen by live occupancy.
    """
    discovered = await BleakScanner.discover(timeout=timeout, return_adv=True)

    found: dict[str, DiscoveredThermostat] = {}
    for device, adv in discovered.values():
        name = device.name or adv.local_name or ""
        if NAME_SUBSTRING not in name:
            continue

        mac = device.address.upper()
        if mac not in found:
            rssi = adv.rssi if adv.rssi is not None else -100
            found[mac] = DiscoveredThermostat(mac=mac, name=name, rssi=rssi)
            logger.info("Discovered %s (%s) on scan", name, mac)

    return list(found.values())


# ── Transient BLE session ─────────────────────────────────────────────────────

class EasyTouchSession:
    """Transient BLE session to an EasyTouch RV controller.

    Connects on entry, disconnects on exit.  Performs the passcode handshake
    (if a passcode is supplied) after connecting.

    Usage::

        async with EasyTouchSession(mac, passcode="...") as s:
            status = await s.get_status(zone=0)
            await s.change(zone=0, mode=MODE_COOL, cool_sp=74)
    """

    # Scan attempts made to resolve the device before giving up; each
    # attempt is one BleakScanner discovery window.
    FIND_ATTEMPTS = 3
    FIND_TIMEOUT = 15.0

    def __init__(
        self,
        address: str,
        passcode: str = "",
    ):
        self._address = address
        self._passcode = passcode or ""
        self._client: BleakClient | None = None
        self._time_syncs_left = 3
        self.ble_name: str = ""    # advertised name captured during scanning

    async def __aenter__(self) -> "EasyTouchSession":
        logger.info("[%s] Scanning...", self._address)
        device = None
        for attempt in range(self.FIND_ATTEMPTS):
            device = await BleakScanner.find_device_by_address(
                self._address, timeout=self.FIND_TIMEOUT)
            if device is not None:
                break
            logger.debug("[%s] Not seen in scan attempt %d/%d",
                         self._address, attempt + 1, self.FIND_ATTEMPTS)
        if device is None:
            raise ConnectionError(f"[{self._address}] Device not found after scanning")
        self.ble_name = device.name or ""

        logger.info("[%s] Connecting...", self._address)
        # The catcher routes each attempt to a claim-aware adapter and runs
        # the validator once the link is up: a link without the vendor
        # return characteristic is torn down, scored against that adapter,
        # and retried on the next radio inside establish_connection —
        # v1's validate_connection semantics.  tolerate_late_gatt keeps
        # v1's escalating waits for chips that register vendor services
        # after ServicesResolved.
        self._client = await establish_connection(
            BleakClient,
            device,
            "EasyTouch %s" % self._address,
            max_attempts=4,
            validate_connection=tolerate_late_gatt(
                validate_char_exists(RETURN_UUID)),
        )
        # From here on the connection is live: any failure must disconnect
        # before propagating, because __aexit__ is not called when
        # __aenter__ raises.
        try:
            logger.info("[%s] Connected (MTU: %d)", self._address, self._client.mtu_size)
            if self._passcode:
                await self._authenticate()
        except BaseException:
            await self.__aexit__()
            raise

        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._client is not None:
            try:
                await asyncio.wait_for(self._client.disconnect(), timeout=5.0)
                logger.debug("[%s] Disconnected", self._address)
            except Exception:
                logger.debug("[%s] Disconnect failed", self._address, exc_info=True)
            self._client = None

    # ── Passcode handshake ────────────────────────────────────────────

    async def _authenticate(self) -> bool:
        """Write the passcode to DD01 and check the reply.

        Raises PermissionError on an explicit 'Invalid' rejection — the unit
        would silently ignore commands for the rest of the session, so
        continuing is pointless.  Transport hiccups and unexpected replies
        are tolerated (the session proceeds; commands may still work).
        """
        if self._client is None:
            return False
        try:
            await self._client.write_gatt_char(
                PASSWORD_UUID, self._passcode.encode("utf-8"), response=False
            )
            await asyncio.sleep(0.2)
            raw = await self._client.read_gatt_char(PASSWORD_UUID)
            reply = bytes(raw).decode("utf-8", errors="replace")
        except Exception:
            logger.exception("[%s] Passcode handshake failed", self._address)
            return False

        if "Matched" in reply:
            logger.debug("[%s] Passcode accepted", self._address)
            return True
        if "Invalid" in reply:
            raise PermissionError(
                "[%s] Passcode rejected by device" % self._address)
        logger.warning("[%s] Unexpected passcode reply: %r", self._address, reply)
        return False

    # ── JSON exchange ─────────────────────────────────────────────────

    async def _exchange(self, command: dict, timeout: float = 6.0,
                        expect_rt: str | None = None) -> dict | None:
        """Write a JSON command, then read the JSON response from RETURN_UUID.

        ``0xFF01`` is a plain read characteristic that holds the *last*
        response the unit produced, so a read taken before the unit has
        answered returns a stale message (typically the previous Status).
        When ``expect_rt`` is given, keep polling until a response with that
        ``RT`` arrives, so replies are correlated with their request.
        """
        # Deliberately not gated on client.is_connected: after an adapter
        # recovery/reset (e.g. the catcher's scanner watchdog), BlueZ's
        # device object is removed and no property signal follows, so the
        # client's cached is_connected is stranded and no longer reflects
        # reality.  Attempt
        # the operation and let a real failure surface instead -- an
        # unanswered exchange trips the monitor's missed-status counter,
        # which tears the session down and reconnects.
        if self._client is None:
            return None

        payload = json.dumps(command).encode("utf-8")
        try:
            # Write WITHOUT response: the firmware rejects write-with-response
            # (BlueZ turns >MTU-3 payloads into a prepared/long write, which
            # the unit refuses with GATT "Unlikely Error" and then drops the
            # link).  MTU stays at 23 on BlueZ; plain write commands with the
            # full payload are accepted (verified on GE 347 units).
            await self._client.write_gatt_char(CMD_UUID, payload, response=False)
        except Exception:
            logger.exception("[%s] Command write failed: %s",
                             self._address, command.get("Type"))
            return None

        # Poll the return characteristic for the response.  The unit answers
        # within a second or two; retry the read a few times before giving up.
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                raw = await self._client.read_gatt_char(RETURN_UUID)
            except Exception:
                logger.debug("[%s] Return read failed, retrying",
                             self._address, exc_info=True)
                await asyncio.sleep(0.3)
                continue

            text = bytes(raw).decode("utf-8", errors="replace").strip()
            if not text:
                await asyncio.sleep(0.3)
                continue
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                logger.debug("[%s] Non-JSON return payload: %r", self._address, text)
                await asyncio.sleep(0.3)
                continue

            # The unit rejects unknown message types with an Invalid wrapper.
            if "Invalid" in obj and "Sent_type" in obj:
                logger.warning("[%s] Unit rejected %r: %s", self._address,
                               obj.get("Sent_type"), obj.get("Invalid"))
                return obj

            if expect_rt is not None and obj.get("RT") != expect_rt:
                # Stale or unrelated response still in the buffer; keep polling.
                logger.debug("[%s] Waiting for RT=%s, saw RT=%r",
                             self._address, expect_rt, obj.get("RT"))
                await asyncio.sleep(0.3)
                continue
            return obj

        logger.warning("[%s] No response to %s (%.0fs timeout)",
                       self._address, command.get("Type"), timeout)
        return None

    # ── High-level commands ───────────────────────────────────────────

    async def get_status(self, zone: int = 0) -> ZoneStatus | None:
        """Request and decode the status for one zone."""
        cmd = {"Type": "Get Status", "Zone": zone, "TM": int(time.time())}
        obj = await self._exchange(cmd, expect_rt="Status")
        if obj is None:
            return None
        if obj.get("RT") not in ("Status", None) and obj.get("Type") != "Response":
            logger.debug("[%s] Non-status reply: %r", self._address, obj)
        zones = _parse_status(obj)
        return zones.get(zone) or (next(iter(zones.values()), None))

    async def get_config(self, zone: int = 0) -> ZoneConfig | None:
        """Request and decode the capability config for one zone."""
        obj = await self._exchange({"Type": "Get Config", "Zone": zone},
                                   expect_rt="Config")
        if obj is None:
            return None
        return _parse_config(obj)

    async def get_extra_data(self, zone: int = 0,
                             model_prefix: str = "") -> ExtraData | None:
        """Request and decode the model-specific ExtraData diagnostics."""
        obj = await self._exchange({"Type": "ExtraData", "Zone": zone},
                                   expect_rt="ExtraData")
        if obj is None:
            return None
        if obj.get("RT") not in ("ExtraData", None):
            logger.debug("[%s] Non-ExtraData reply: %r", self._address, obj)
        return parse_extra_data(obj, model_prefix)

    async def change(self, zone: int, **changes) -> bool:
        """Apply one or more changes to a zone.

        Accepts change keys such as ``mode``, ``power``, ``cool_sp``,
        ``heat_sp``, ``dry_sp``, ``autoCool_sp``, ``autoHeat_sp`` and the
        per-mode fan keys.  Returns True on an OK acknowledgement.
        """
        body = {"zone": zone}
        body.update(changes)
        obj = await self._exchange({"Type": "Change", "Changes": body},
                                   expect_rt="OK")
        if obj is None:
            return False
        rt = obj.get("RT")
        ok = rt == "OK" or obj.get("Type") == "Response"
        if not ok:
            logger.warning("[%s] Change not acknowledged: %r", self._address, obj)
        return ok

    async def set_mode(self, zone: int, mode: int) -> bool:
        return await self.change(zone, mode=mode)

    async def set_power(self, zone: int, on: bool) -> bool:
        return await self.change(zone, power=1 if on else 0)

    async def set_setpoints(
        self, zone: int,
        cool_sp: int | None = None,
        heat_sp: int | None = None,
    ) -> bool:
        """Set cool and/or heat setpoints for a zone."""
        changes: dict[str, int] = {}
        if cool_sp is not None:
            changes["cool_sp"] = int(cool_sp)
        if heat_sp is not None:
            changes["heat_sp"] = int(heat_sp)
        if not changes:
            return True
        return await self.change(zone, **changes)


# ── Snapshots and monitoring ─────────────────────────────────────────────────

@dataclass
class DeviceSnapshot:
    """Everything read from a device in one monitor cycle.

    ``applied_changes`` counts queued change batches consumed this cycle
    (applied or expired) so the caller can settle its in-flight bookkeeping.
    """
    status: ZoneStatus | None = None
    config: ZoneConfig | None = None
    extra: ExtraData | None = None
    ble_name: str = ""
    applied_changes: int = 0


async def poll_device(
    address: str,
    zone: int = 0,
    passcode: str = "",
    need_config: bool = False,
    model_prefix: str = "",
    want_extra: bool = True,
) -> DeviceSnapshot:
    """Connect and read status (plus config/diagnostics) in one session.

    ``model_prefix`` selects the ExtraData decoder; when empty it is derived
    from the advertised BLE name seen during scanning.  Designed for
    run_coroutine_threadsafe; returns an empty snapshot on failure.
    """
    try:
        async with EasyTouchSession(address, passcode) as s:
            snap = DeviceSnapshot(ble_name=s.ble_name)
            prefix = model_prefix or model_prefix_from_name(s.ble_name)
            if need_config:
                snap.config = await s.get_config(zone)
            snap.status = await s.get_status(zone)
            if want_extra and snap.status is not None:
                snap.extra = await s.get_extra_data(zone, prefix)
            return snap
    except Exception:
        logger.exception("[%s] Poll failed", address)
        return DeviceSnapshot()


# ── Device monitor ────────────────────────────────────────────────────────────
#
# One long-lived asyncio task per thermostat owns the BLE connection and all
# traffic to it: it applies queued changes, polls status on a fixed cadence,
# reads diagnostics periodically, and reconnects with exponential backoff.
# This mirrors the vendor app's connection model (persistent central) while
# still supporting a transient mode (connect per cycle) for users who need
# the BLE link free for the phone app between polls.

# Queued changes older than this are dropped instead of applied late — a
# "power off" landing many minutes after the tap is worse than dropping it.
CHANGE_MAX_AGE = 120.0

# Consecutive unanswered status requests before the connection is considered
# wedged and torn down for a reconnect (vendor app uses the same count).
MAX_MISSED_STATUS = 3

# Post-change settle delay before the read-back status request.
CHANGE_SETTLE_DELAY = 1.0


@dataclass
class MonitorSettings:
    """Live-tunable monitor behaviour.

    The monitor reads these attributes on every cycle, so a controller
    (e.g. the D-Bus service, driven from GUI settings) may mutate them at
    runtime: flipping ``persistent`` drops or keeps the connection at the
    next cycle boundary, and ``poll_seconds`` takes effect on the next
    wait.  ``poll_seconds`` is the status cadence while connected in
    persistent mode, and the between-sessions interval in transient mode.
    """
    persistent: bool = True
    poll_seconds: float = 10.0
    diagnostics_seconds: float = 60.0
    reconnect_base: float = 30.0
    reconnect_max: float = 900.0


def _drain_changes(queue: asyncio.Queue, pending: list,
                   max_age: float = CHANGE_MAX_AGE,
                   now: float | None = None) -> tuple[dict, int, int]:
    """Merge all queued (timestamp, changes) batches into one Change body.

    Later batches override earlier keys.  Batches older than ``max_age``
    are dropped.  Returns (merged, consumed_batches, dropped_batches).
    """
    items = list(pending)
    pending.clear()
    while True:
        try:
            items.append(queue.get_nowait())
        except asyncio.QueueEmpty:
            break
    if now is None:
        now = time.monotonic()
    merged: dict = {}
    dropped = 0
    for ts, changes in items:
        if now - ts > max_age:
            dropped += 1
            logger.warning("Dropping stale change (%.0fs old): %r",
                           now - ts, changes)
            continue
        merged.update(changes)
    return merged, len(items), dropped


async def run_device_monitor(
    address: str,
    zone: int = 0,
    passcode: str = "",
    model_prefix: str = "",
    *,
    change_queue: asyncio.Queue,
    on_snapshot,
    settings: MonitorSettings | None = None,
    session_factory=EasyTouchSession,
) -> None:
    """Supervise one thermostat until cancelled.

    ``change_queue`` carries (time.monotonic(), {change keys}) tuples from
    the control side; ``on_snapshot`` is called (in the event loop thread)
    with a DeviceSnapshot after every cycle, and with an empty snapshot when
    the connection is lost.  ``settings`` is re-read every cycle, so it may
    be mutated at runtime; enqueue an empty change batch to wake the monitor
    early after changing it.  Cancel the task to stop monitoring.
    """
    if settings is None:
        settings = MonitorSettings()
    pending: list = []          # changes carried across wakeups/reconnects
    unreported = 0              # consumed batches not yet reported upstream
    backoff_attempt = 0

    while True:
        try:
            async with session_factory(address, passcode) as s:
                backoff_attempt = 0
                prefix = model_prefix or model_prefix_from_name(s.ble_name)
                cfg = await s.get_config(zone)
                first_cycle = True
                missed = 0
                last_diag = 0.0

                while True:
                    merged, consumed, _dropped = _drain_changes(
                        change_queue, pending)
                    unreported += consumed
                    if merged:
                        # Logged so wire traffic is verifiable: several
                        # queued batches collapse into one Change here.
                        logger.info("[%s] Applying change (%d queued): %r",
                                    address, consumed, merged)
                        await s.change(zone, **merged)
                        await asyncio.sleep(CHANGE_SETTLE_DELAY)

                    st = await s.get_status(zone)
                    extra = None
                    now = time.monotonic()
                    if st is not None and \
                            now - last_diag >= settings.diagnostics_seconds:
                        extra = await s.get_extra_data(zone, prefix)
                        last_diag = now

                    on_snapshot(DeviceSnapshot(
                        status=st,
                        config=cfg if first_cycle else None,
                        extra=extra,
                        ble_name=s.ble_name,
                        applied_changes=unreported,
                    ))
                    unreported = 0
                    first_cycle = False

                    if st is None:
                        missed += 1
                        if missed >= MAX_MISSED_STATUS:
                            raise ConnectionError(
                                "%d consecutive unanswered status requests"
                                % missed)
                    else:
                        missed = 0

                    if not settings.persistent:
                        break

                    # Sleep until the next poll, waking early for a change.
                    try:
                        item = await asyncio.wait_for(
                            change_queue.get(),
                            timeout=settings.poll_seconds)
                        pending.append(item)
                    except asyncio.TimeoutError:
                        pass

            # Transient mode: idle disconnected until the next cycle is due,
            # reconnecting immediately when a change request arrives.
            try:
                item = await asyncio.wait_for(
                    change_queue.get(), timeout=settings.poll_seconds)
                pending.append(item)
            except asyncio.TimeoutError:
                pass

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[%s] Monitor connection lost: %s", address, exc)
            logger.debug("[%s] Monitor traceback", address, exc_info=True)
            on_snapshot(DeviceSnapshot(applied_changes=unreported))
            unreported = 0
            delay = min(settings.reconnect_base * (2 ** backoff_attempt),
                        settings.reconnect_max)
            backoff_attempt += 1
            logger.info("[%s] Reconnecting in %.0fs (attempt %d)",
                        address, delay, backoff_attempt)
            # Wake early if a change arrives so user commands are snappy.
            try:
                item = await asyncio.wait_for(change_queue.get(), timeout=delay)
                pending.append(item)
            except asyncio.TimeoutError:
                pass
