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

from __future__ import annotations

"""
Venus OS service for Micro-Air EasyTouch RV thermostats.

Each configured thermostat zone registers two D-Bus services:

  com.victronenergy.temperature.easytouch_<id>
      Inside temperature as a standard Venus OS temperature sensor
      (shows up in the temperature list and on VRM).

  com.victronenergy.switch.easytouch_<id>
      Thermostat controls as Switchable Outputs, rendered by GUI v2 as a
      single card in the switch pane:
        - Power       toggle    (type 1)  master system power
        - Mode        dropdown  (type 6)  modes advertised by the unit
        - Fan         dropdown  (type 6)  fan choices for the current mode
        - Cool to     slider    (type 3)  cool / dry / auto-cool setpoint
        - Heat to     slider    (type 3)  heat / auto-heat setpoint
      Sliders are Celsius on the wire (GUI converts to the user's unit);
      the device itself is integer Fahrenheit.

Connection model: one supervisor task per thermostat owns a persistent BLE
connection (the vendor app's own model), polling status every few seconds
and diagnostics every minute, applying queued control changes immediately,
and reconnecting with exponential backoff.  Control writes are coalesced
for one second (like the vendor app) before being queued.  Set
``connection = transient`` in config.ini to connect per-poll instead,
leaving the BLE link free for the phone app between polls.

Devices are loaded from config.ini (see config.default.ini / provision.py).
"""

import asyncio
import concurrent.futures
import configparser
import logging
import os
import signal
import sys
import threading
import time

import dbus
from gi.repository import GLib
from dbus.mainloop.glib import DBusGMainLoop

# Add ext folders to sys.path
_ext_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
sys.path.insert(1, os.path.join(_ext_dir, "velib_python"))

# All BLE dependencies from local ext/ submodules (upstream repos)
for _sub in [
    os.path.join(_ext_dir, "bleak-connection-manager", "src"),
    os.path.join(_ext_dir, "bleak-retry-connector", "src"),
    os.path.join(_ext_dir, "bluetooth-adapters", "src"),
    os.path.join(_ext_dir, "aiooui", "src"),
    os.path.join(_ext_dir, "bleak"),
]:
    if os.path.isdir(_sub) and _sub not in sys.path:
        sys.path.insert(0, _sub)

from vedbus import VeDbusService  # noqa: E402
from settingsdevice import SettingsDevice  # noqa: E402

from bleak_connection_manager import install_bleak_catcher  # noqa: E402


def _catcher_options() -> tuple[list[str], dict[str, int]]:
    """Adapter entries and link caps for the bleak catcher, from config.ini.

    Read here, ahead of the manager's full config load, because the catcher
    must be installed before easytouch_ble is imported (that module captures
    bleak.BleakClient / bleak.BleakScanner at import time).
    """
    cfg = configparser.ConfigParser()
    config_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ("config.ini", "config.default.ini"):
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            cfg.read(path)
            break
    defaults = cfg["DEFAULT"] if "DEFAULT" in cfg else {}
    # Raw entries, passed to the catcher verbatim: "MAC@hciX" pins a device
    # to an adapter, plain "hciX" entries form the shared pool.  Empty means
    # every adapter the kernel exposes is a candidate.
    adapters = defaults.get("adapters", "").replace(",", " ").split()
    # "hciX:N" caps established links on that adapter (deployment config
    # for dongles with undocumented limits, e.g. CSR ~5).
    link_caps: dict[str, int] = {}
    for entry in defaults.get("link_caps", "").replace(",", " ").split():
        adapter, _, cap = entry.partition(":")
        if adapter and cap.isdigit():
            link_caps[adapter] = int(cap)
    return adapters, link_caps


_adapters, _link_caps = _catcher_options()
install_bleak_catcher(
    "dbus-easytouchrv",
    adapters=_adapters,
    link_caps=_link_caps,
    wrap_scanner=True,
)

from easytouch_ble import (  # noqa: E402
    run_device_monitor,
    MonitorSettings,
    DeviceSnapshot,
    ExtraData,
    ZoneStatus,
    ZoneConfig,
    MODE_NAMES,
    MODE_OFF,
    MODE_DRY,
    MODE_ZONE_OFF,
    HEAT_MODES,
    AUTO_MODES,
    MODE_COOL,
    fan_change_key,
    fault_name,
    fan_options,
    fan_option_index,
    model_prefix_from_name,
    FAN_WRITE_MASK,
)

VERSION = "0.2.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("dbus-easytouchrv")

# Defaults
DEFAULT_POLL_SECONDS = 10            # persistent mode: in-connection cadence
DEFAULT_DIAGNOSTICS_SECONDS = 60     # ExtraData cadence
DEFAULT_POLL_INTERVAL_MINUTES = 5    # transient mode: time between sessions

# Base D-Bus device instance.  Each thermostat reserves a block of
# INSTANCE_STRIDE instances: base+0 = main sensor & switch service,
# base+1 = inside-coil sensor, base+2 = outside-coil sensor,
# base+3 = outside-air sensor.  A fixed stride keeps instances stable
# across restarts and lazy creation.
DEVICE_INSTANCE_BASE = 150
INSTANCE_STRIDE = 5
INSTANCE_OFFSET_IN_COIL = 1
INSTANCE_OFFSET_OUT_COIL = 2
INSTANCE_OFFSET_OUTSIDE = 3
INSTANCE_OFFSET_FREEZE = 4

# Virtual digital input used to get freeze alarms into VRM's alarm log.
# Venus only ships whitelisted paths to VRM: a temperature service logs
# just /Alarms/LowBattery, so our /Alarms/LowTemperature never reaches it.
# digitalinput /Alarm *is* logged (code "dia"), so the freeze condition is
# mirrored onto a virtual input.  Type 10 = "Generic I/O" -- the only
# neutral type; the rest are Door/Bilge/Burglar/Smoke/Fire/CO2, which would
# label a freeze as something it is not.
DIGITAL_INPUT_PRODUCT_ID = 0xA166
DIGITAL_INPUT_TYPE_GENERIC_IO = 10
DI_ALARM_OK = 0
# Venus alarm levels are 0 = ok, 1 = warning, 2 = alarm.  A frozen coil is
# self-correcting (the unit stops the compressor and defrosts), so it is
# reported as a warning: logged and visible, but not an audible alarm.
# Note Victron's own digital-input driver only ever emits 0 or 2; 1 follows
# the documented convention rather than that driver's usage.
DI_ALARM_ACTIVE = 1

# /TemperatureType values (see Venus OS dbus docs).
TEMP_TYPE_GENERIC = 2
TEMP_TYPE_ROOM = 3
TEMP_TYPE_OUTDOOR = 4

# Reconnect backoff after a failed BLE operation.
RECONNECT_BASE_DELAY = 30      # seconds
RECONNECT_MAX_DELAY = 900      # 15 minutes

# Control writes are coalesced for this long before one BLE session
# applies them all (mirrors the vendor app's behaviour).  The timer
# restarts on each write, so a burst collapses into one message.
CHANGE_COALESCE_MS = 1000

# Floor on the interval between dispatches for one device.  A slow drag
# pauses for more than the coalescing window repeatedly, which would each
# time flush another BLE write; this caps that to one dispatch per window
# (always carrying the latest values) without adding latency to a single
# tap, whose previous dispatch is long past.
MIN_DISPATCH_INTERVAL_MS = 3000

# Temperature-sensor connection status codes.
SENSOR_OK = 0
SENSOR_DISCONNECTED = 4

# SwitchableOutput status codes.
SW_STATUS_OFF = 0x00
SW_STATUS_ON = 0x09
SW_STATUS_DISABLED = 0x20

# SwitchableOutput types.
SW_TYPE_TOGGLE = 1
SW_TYPE_TEMPERATURE_SETPOINT = 3
SW_TYPE_DROPDOWN = 6
SW_TYPE_BASIC_SLIDER = 7

# Fault codes that mean the unit stopped (or is about to) on a freeze.
FAULT_FREEZE_DETECTED = 5

# Severity per fault code (Venus: 1 = warning, 2 = alarm).  Sensor faults and
# conditions that are normal in RV use (no shore power while boondocking, a
# frozen coil that clears when the unit cycles) are warnings; anything that
# means the A/C cannot run or needs service is an alarm.  Codes not listed
# default to alarm.
FAULT_SEVERITY = {
    1:  2,   # NO COMMUNICATION   - thermostat cannot reach the A/C
    2:  1,   # BAD REMOTE SENSOR
    3:  1,   # BAD OUTSIDE SENSOR
    4:  1,   # BAD FREEZE SENSOR
    5:  1,   # FREEZE DETECTED    - reported via /Alarms/LowTemperature
    6:  1,   # BAD HUMIDITY SENSOR
    7:  1,   # NO AC POWER        - normal when off shore power
    8:  2,   # INVALID CONFIG
    9:  1,   # LOW DC VOLTAGE     - the GX monitors the battery properly
    10: 1,   # BAD INDOOR SENSOR
    11: 1,   # INSIDE COIL SENSOR
    12: 1,   # OUTSIDE COIL SENSOR
    13: 2,   # LOW REFRIGERANT    - needs service
}

# Freeze warnings come solely from the unit's own FREEZE DETECTED fault --
# the A/C manufacturer's engineered trip point for that coil and airflow
# design.  A driver-side coil-temperature threshold was tried and removed:
# these evaporators routinely sit below freezing during normal cooling, so
# any guessed threshold produces false warnings.  The coil temperature is
# still published as its own sensor for anyone who wants to build a custom
# rule (Node-RED, VRM alarms) on top of it.

# GUI-adjustable poll interval bounds (seconds).
POLL_SECONDS_MIN = 5
POLL_SECONDS_MAX = 600

# Synthetic dropdown entry: the thermostat's master power lives in the mode
# dropdown ("Off" = powered down, any other choice powers on and sets mode).
MODE_ENTRY_OFF = -1

# Modes for which the cool / heat sliders are shown.
_COOL_SLIDER_MODES = {MODE_COOL, MODE_DRY} | AUTO_MODES
_HEAT_SLIDER_MODES = HEAT_MODES | AUTO_MODES


def f_to_c(f: float | None) -> float | None:
    """Convert Fahrenheit to Celsius (Venus OS wire convention)."""
    if f is None:
        return None
    return round((f - 32.0) * 5.0 / 9.0, 1)


def f_to_c_exact(f: float | None) -> float | None:
    """Fahrenheit to Celsius without rounding.

    Setpoint sliders must land exactly on whole degrees Fahrenheit (the only
    resolution the thermostat accepts), so their bounds, step and value are
    published at full precision -- rounding to 0.1 C would put the steps
    slightly off whole degrees and the handle between stops.
    """
    if f is None:
        return None
    return (f - 32.0) * 5.0 / 9.0


# One degree Fahrenheit expressed in Celsius: the slider's step size.
STEP_ONE_DEGREE_F = 5.0 / 9.0


def c_to_f(c: float | None) -> int | None:
    """Convert Celsius back to the device's integer Fahrenheit."""
    if c is None:
        return None
    return int(round(c * 9.0 / 5.0 + 32.0))


def new_private_bus():
    """A private bus connection.

    Every VeDbusService exports its object tree from '/' on its connection,
    so each service needs its own connection or the object paths collide.
    """
    if "DBUS_SESSION_BUS_ADDRESS" in os.environ:
        return dbus.SessionBus(private=True)
    return dbus.SystemBus(private=True)


def load_config() -> configparser.ConfigParser:
    """Load configuration from config.ini, falling back to config.default.ini."""
    config = configparser.ConfigParser()
    config_dir = os.path.dirname(os.path.abspath(__file__))
    config_file = os.path.join(config_dir, "config.ini")
    default_file = os.path.join(config_dir, "config.default.ini")

    if os.path.exists(config_file):
        config.read(config_file)
        logger.info("Loaded config from %s", config_file)
    elif os.path.exists(default_file):
        config.read(default_file)
        logger.info("Loaded default config from %s", default_file)
    else:
        logger.warning("No config file found, using defaults")
    return config


def get_device_configs(config: configparser.ConfigParser) -> dict[str, dict[str, str]]:
    """Extract per-thermostat configuration from named sections, keyed by MAC."""
    devices: dict[str, dict[str, str]] = {}
    for section in config.sections():
        mac = config.get(section, "mac", fallback="").strip().upper()
        if not mac or ":" not in mac:
            continue
        if mac in devices:
            # Multi-zone (several sections sharing one unit's MAC) is not
            # supported: one BLE central per unit means one section per MAC.
            logger.warning(
                "Config section [%s] reuses MAC %s already defined by "
                "another section; ignoring it (multi-zone via duplicate "
                "sections is not supported)", section, mac)
            continue
        devices[mac] = {
            "name": config.get(section, "name", fallback=section),
            "zone": config.get(section, "zone", fallback="0"),
            "passcode": config.get(section, "passcode", fallback=""),
            "serial": config.get(section, "serial", fallback="").strip(),
        }
    return devices


# ── Per-thermostat D-Bus device ───────────────────────────────────────────────

class ThermostatDevice:
    """One EasyTouch zone: a temperature service plus a switch service."""

    def __init__(self, manager: "EasyTouchManager", mac: str, name: str,
                 zone: int, passcode: str, instance: int, serial: str = "",
                 monitor_settings: MonitorSettings | None = None):
        self.manager = manager
        self.mac = mac
        self.name = name
        self.zone = zone
        self.passcode = passcode
        self.instance = instance
        self.serial = serial
        self.model_prefix = serial[:3] if len(serial) >= 3 else ""
        self.dev_id = mac.replace(":", "").lower() + ("_z%d" % zone if zone else "")
        self.monitor_settings = monitor_settings or MonitorSettings()

        # Set by the manager when the monitor task is started.
        self.change_queue = None
        self.monitor_future = None

        self.config: ZoneConfig | None = None
        self.last_status: ZoneStatus | None = None

        # Lazily-created auxiliary sensors (coils, outside air), keyed by kind.
        self._aux: dict[str, VeDbusService] = {}
        # True while ExtraData supplies the outside-air reading, so a status
        # payload without one (sentinel) doesn't flap the value to None.
        self._extra_has_oat = False
        # Alarm state (published only on change).
        self._alarm_low = 0
        self._alarm_fault = 0
        self._last_fault = 0
        self._last_coil_f: int | None = None

        # Control-write coalescing (all touched from the GLib main thread).
        self._pending: dict[str, int] = {}
        self._pending_timer: int | None = None
        self._inflight = 0
        # None means "never dispatched" -- distinct from "dispatched at
        # monotonic time 0", which would rate-limit the very first change
        # (time.monotonic()'s epoch is arbitrary and can be near zero).
        self._last_dispatch_ms: float | None = None

        # Switchable-output bookkeeping, populated once config is known.
        self._outputs_created = False
        self._mode_values: list[int] = []      # dropdown index -> mode id
        self._fan_opts: list[tuple[str, int]] = []
        self._fan_opts_mode: int | None = None

        # ── Temperature sensor service ─────────────────────────────────
        self._temp = VeDbusService(
            "com.victronenergy.temperature.easytouch_%s" % self.dev_id,
            new_private_bus(), register=False,
        )
        svc = self._temp
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", VERSION)
        svc.add_path("/Mgmt/Connection", "EasyTouch BLE %s" % mac)
        svc.add_path("/DeviceInstance", instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", "EasyTouch RV Thermostat")
        svc.add_path("/CustomName",
                     manager.custom_name(self.dev_id, "main", name),
                     writeable=True,
                     onchangecallback=self._make_name_cb("main"))
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/HardwareVersion", None)
        svc.add_path("/Connected", 1)
        svc.add_path("/TemperatureType", TEMP_TYPE_ROOM)
        svc.add_path("/Temperature", None)           # inside temperature, C
        svc.add_path("/Status", SENSOR_DISCONNECTED)

        # Informational (read-only) extras.
        svc.add_path("/OutsideTemperature", None)    # C
        svc.add_path("/ModeName", "")
        svc.add_path("/RunningState", "")
        svc.add_path("/Fault", "")

        # Diagnostics from ExtraData (availability depends on A/C model).
        svc.add_path("/FaceplateTemperature", None)  # C
        svc.add_path("/RoomSensorTemperature", None) # C (Atwood remote sensor)
        svc.add_path("/WeatherTemperature", None)    # C (cloud weather, wifi units)
        svc.add_path("/Voltage", None)               # supply V (GE)
        svc.add_path("/DiagnosticFlags", "")         # comma-separated active flags

        # Venus alarm convention: 0 = OK, 1 = warning, 2 = alarm.
        # /Alarms/LowTemperature fires on a freeze condition (the unit's own
        # FREEZE DETECTED fault, or a coil at/below freezing), which is what
        # shuts an A/C down; /Alarms/Fault covers every other fault code.
        svc.add_path("/Alarms/LowTemperature", 0)
        svc.add_path("/Alarms/Fault", 0)
        svc.register()

        # ── Switch (controls) service ──────────────────────────────────
        self._switch = VeDbusService(
            "com.victronenergy.switch.easytouch_%s" % self.dev_id,
            new_private_bus(), register=False,
        )
        svc = self._switch
        svc.add_path("/Mgmt/ProcessName", __file__)
        svc.add_path("/Mgmt/ProcessVersion", VERSION)
        svc.add_path("/Mgmt/Connection", "EasyTouch BLE %s" % mac)
        svc.add_path("/DeviceInstance", instance)
        svc.add_path("/ProductId", 0xFFFF)
        svc.add_path("/ProductName", "EasyTouch RV Thermostat")
        svc.add_path("/CustomName",
                     manager.custom_name(self.dev_id, "switch", name),
                     writeable=True,
                     onchangecallback=self._make_name_cb("switch"))
        svc.add_path("/FirmwareVersion", None)
        svc.add_path("/HardwareVersion", None)
        svc.add_path("/Connected", 1)
        svc.add_path("/State", 0x100)                # module-level: connected
        # Rendered as "Module voltage" on the switch device page; fed from
        # the thermostat's own supply-voltage reading (GE units report VDC).
        svc.add_path("/ModuleVoltage", None)

        # Driver setup controls: shown on their own card in the switch pane.
        # These configure the monitor, not the thermostat, so they remain
        # usable while the device is unreachable.
        self._add_output_common(svc, "connection", "Connection",
                                SW_TYPE_DROPDOWN)
        svc.add_path(self._out("connection", "Settings/Labels"),
                     ["Persistent", "Intermittent"])
        svc.add_path(self._out("connection", "Dimming"),
                     0 if self.monitor_settings.persistent else 1,
                     writeable=True,
                     onchangecallback=self._on_connection_write)
        # The GUI only renders an output's value when its status is On, so
        # these driver-setup controls are always "on" -- they configure the
        # driver and stay usable even when the thermostat is unreachable.
        svc["/SwitchableOutput/connection/Status"] = SW_STATUS_ON

        svc.register()

        # ── Virtual digital input: freeze alarm for VRM ────────────────
        self._freeze_input = None
        if manager.freeze_input_enabled:
            self._create_freeze_input()

        logger.info("Registered thermostat '%s' (%s zone %d) instance %d",
                    name, mac, zone, instance)

    # ── Switchable-output construction (after config arrives) ──────────

    def _out(self, output: str, tail: str) -> str:
        return "/SwitchableOutput/%s/%s" % (output, tail)

    def _add_output_common(self, ctx, output: str, name: str, sw_type: int,
                           group: str | None = None):
        ctx.add_path(self._out(output, "Name"), name)
        ctx.add_path(self._out(output, "Status"), SW_STATUS_OFF)
        ctx.add_path(self._out(output, "Settings/CustomName"), "", writeable=True)
        ctx.add_path(self._out(output, "Settings/Type"), sw_type, writeable=True)
        ctx.add_path(self._out(output, "Settings/ValidTypes"), 1 << sw_type)
        ctx.add_path(self._out(output, "Settings/Group"),
                     group if group is not None else self.name, writeable=True)
        ctx.add_path(self._out(output, "Settings/ShowUIControl"), 1, writeable=True)

    def apply_config(self, cfg: ZoneConfig):
        """Create the switchable outputs once the zone's capabilities are known."""
        self.config = cfg
        if self._outputs_created:
            return
        if cfg.available_modes == 0:
            logger.warning("[%s] Zone %d reports no available modes; "
                           "controls not created", self.dev_id, cfg.zone)
            return

        # "Off" heads the dropdown and means master power off; the unit's
        # own mode 0 is folded into it rather than listed separately.
        modes = [m for m in range(16)
                 if cfg.mode_available(m) and m in MODE_NAMES
                 and m not in (MODE_OFF, MODE_ZONE_OFF)]
        self._mode_values = [MODE_ENTRY_OFF] + modes
        mode_labels = ["Off"] + [MODE_NAMES[m] for m in modes]

        available = set(modes)
        want_cool = bool(available & _COOL_SLIDER_MODES)
        want_heat = bool(available & _HEAT_SLIDER_MODES)
        want_fan = any(fan_options(cfg.fan_array[m])
                       for m in available
                       if fan_change_key(m) and m < len(cfg.fan_array))

        with self._switch as ctx:
            # Mode dropdown (includes Off; master power rides along)
            self._add_output_common(ctx, "mode", "Mode", SW_TYPE_DROPDOWN)
            ctx.add_path(self._out("mode", "Settings/Labels"), mode_labels)
            ctx.add_path(self._out("mode", "Dimming"), 0, writeable=True,
                         onchangecallback=self._on_mode_write)

            # Fan dropdown (labels depend on the current mode)
            if want_fan:
                self._add_output_common(ctx, "fan", "Fan", SW_TYPE_DROPDOWN)
                ctx.add_path(self._out("fan", "Settings/Labels"), [])
                ctx.add_path(self._out("fan", "Dimming"), 0, writeable=True,
                             onchangecallback=self._on_fan_write)

            # Setpoint sliders (Celsius on the wire)
            if want_cool:
                self._add_output_common(
                    ctx, "cool", "Cool to", SW_TYPE_TEMPERATURE_SETPOINT)
                ctx.add_path(self._out("cool", "Settings/DimmingMin"),
                             f_to_c_exact(cfg.min_cool))
                ctx.add_path(self._out("cool", "Settings/DimmingMax"),
                             f_to_c_exact(cfg.max_cool))
                ctx.add_path(self._out("cool", "Settings/StepSize"),
                             STEP_ONE_DEGREE_F)
                ctx.add_path(self._out("cool", "Settings/Decimals"), 0)
                # Declares the wire unit as Celsius so the device-list page
                # converts to the user's unit; the switch card's slider
                # already assumes Celsius regardless.
                ctx.add_path(self._out("cool", "Settings/Unit"), "/Temperature")
                ctx.add_path(self._out("cool", "Measurement"), None)
                ctx.add_path(self._out("cool", "Dimming"),
                             f_to_c_exact(round((cfg.min_cool + cfg.max_cool) / 2.0)),
                             writeable=True,
                             onchangecallback=self._on_cool_write)
            if want_heat:
                self._add_output_common(
                    ctx, "heat", "Heat to", SW_TYPE_TEMPERATURE_SETPOINT)
                ctx.add_path(self._out("heat", "Settings/DimmingMin"),
                             f_to_c_exact(cfg.min_heat))
                ctx.add_path(self._out("heat", "Settings/DimmingMax"),
                             f_to_c_exact(cfg.max_heat))
                ctx.add_path(self._out("heat", "Settings/StepSize"),
                             STEP_ONE_DEGREE_F)
                ctx.add_path(self._out("heat", "Settings/Decimals"), 0)
                # Declares the wire unit as Celsius so the device-list page
                # converts to the user's unit; the switch card's slider
                # already assumes Celsius regardless.
                ctx.add_path(self._out("heat", "Settings/Unit"), "/Temperature")
                ctx.add_path(self._out("heat", "Measurement"), None)
                ctx.add_path(self._out("heat", "Dimming"),
                             f_to_c_exact(round((cfg.min_heat + cfg.max_heat) / 2.0)),
                             writeable=True,
                             onchangecallback=self._on_heat_write)

        self._outputs_created = True
        logger.info("[%s] Controls created: modes=%s cool=%s heat=%s fan=%s",
                    self.dev_id, mode_labels, want_cool, want_heat, want_fan)

    # ── Control writes (GLib main thread, from GUI / VRM / MQTT) ───────

    def _on_mode_write(self, path, value):
        try:
            idx = int(value)
            mode = self._mode_values[idx]
        except (TypeError, ValueError, IndexError):
            logger.warning("[%s] Ignoring bad mode index %r", self.dev_id, value)
            return False
        if mode == MODE_ENTRY_OFF:
            logger.info("[%s] Mode -> Off (power down)", self.dev_id)
            self.queue_change("power", 0)
            return True
        # Selecting a mode also powers the system on, so one dropdown covers
        # both; the unit ignores a redundant power=1.
        logger.info("[%s] Mode -> %s", self.dev_id, MODE_NAMES[mode])
        self.queue_change("power", 1)
        self.queue_change("mode", mode)
        return True

    def _on_fan_write(self, path, value):
        try:
            idx = int(value)
            fan_value = self._fan_opts[idx][1]
        except (TypeError, ValueError, IndexError):
            logger.warning("[%s] Ignoring bad fan index %r", self.dev_id, value)
            return False
        mode = self._current_mode()
        key = fan_change_key(mode)
        if key is None:
            logger.warning("[%s] No fan control in mode %d", self.dev_id, mode)
            return False
        logger.info("[%s] Fan -> %s (%s=%d)", self.dev_id,
                    self._fan_opts[idx][0], key, fan_value)
        self.queue_change(key, fan_value & FAN_WRITE_MASK)
        return True

    def _on_cool_write(self, path, value):
        try:
            sp = c_to_f(float(value))
        except (TypeError, ValueError):
            return False
        if sp is None:
            return False
        mode = self._current_mode()
        if mode in AUTO_MODES:
            key = "autoCool_sp"
        elif mode == MODE_DRY:
            key = "dry_sp"
        else:
            key = "cool_sp"
        logger.info("[%s] Cool setpoint -> %d F (%s)", self.dev_id, sp, key)
        self.queue_change(key, sp)
        return True

    def _on_heat_write(self, path, value):
        try:
            sp = c_to_f(float(value))
        except (TypeError, ValueError):
            return False
        if sp is None:
            return False
        key = "autoHeat_sp" if self._current_mode() in AUTO_MODES else "heat_sp"
        logger.info("[%s] Heat setpoint -> %d F (%s)", self.dev_id, sp, key)
        self.queue_change(key, sp)
        return True

    # ── Device naming (renameable from the GUI) ────────────────────────

    def _make_name_cb(self, kind: str):
        """Persist a GUI rename so it survives restarts.

        Venus convention: /CustomName is user-writable and remembered by the
        driver; the config.ini name is only the initial default.
        """
        def cb(path, value):
            self.manager.set_custom_name(self.dev_id, kind, str(value))
            logger.info("[%s] Renamed %s -> %r", self.dev_id, kind, str(value))
            return True
        return cb

    # ── Driver setup writes (GLib main thread) ─────────────────────────

    def _on_connection_write(self, path, value):
        try:
            idx = int(value)
        except (TypeError, ValueError):
            return False
        if idx not in (0, 1):
            return False
        persistent = idx == 0
        self.monitor_settings.persistent = persistent
        logger.info("[%s] Connection mode -> %s", self.dev_id,
                    "persistent" if persistent else "transient")
        self.manager.monitor_setting_changed(
            self, "Persistent", 1 if persistent else 0)
        return True

    def _current_mode(self) -> int:
        """The mode further writes should be interpreted against.

        A queued (not yet applied) mode change wins over the last status,
        so "switch to Auto, then adjust the sliders" targets the auto
        setpoints even before the device confirms the new mode.
        """
        if "mode" in self._pending:
            return self._pending["mode"]
        return self.last_status.mode if self.last_status is not None else MODE_OFF

    # ── Change coalescing ───────────────────────────────────────────────

    def queue_change(self, key: str, value: int):
        """Merge a change key and (re)start the coalescing timer (GLib thread)."""
        self._pending[key] = value
        if self._pending_timer is not None:
            GLib.source_remove(self._pending_timer)
        self._pending_timer = GLib.timeout_add(CHANGE_COALESCE_MS, self._flush_changes)

    def _flush_changes(self) -> bool:
        self._pending_timer = None
        if not self._pending:
            return False

        # Rate-limit dispatches: if the previous one was recent (a drag in
        # progress), wait out the remainder and send the latest values then.
        now_ms = time.monotonic() * 1000.0
        if self._last_dispatch_ms is not None:
            wait_ms = int(MIN_DISPATCH_INTERVAL_MS
                          - (now_ms - self._last_dispatch_ms))
            if wait_ms > 0:
                self._pending_timer = GLib.timeout_add(
                    wait_ms, self._flush_changes)
                return False

        changes = dict(self._pending)
        self._pending.clear()
        self._last_dispatch_ms = now_ms
        self._inflight += 1
        self.manager.enqueue_change(self, changes)
        return False

    def change_finished(self, count: int = 1):
        """Called (GLib thread) when queued change batches were consumed."""
        self._inflight = max(0, self._inflight - count)

    @property
    def edits_active(self) -> bool:
        """True while user changes are pending or being applied."""
        return bool(self._pending) or self._pending_timer is not None or self._inflight > 0

    # ── Applying device state to D-Bus (GLib thread) ────────────────────

    def _set(self, service, path, value):
        if path in service:
            service[path] = value

    def apply_status(self, st: ZoneStatus):
        self.last_status = st

        # Temperature service
        t = self._temp
        t["/Status"] = SENSOR_OK
        t["/Temperature"] = f_to_c(st.inside_temp)
        if st.outside_temp is not None:
            self._set(t, "/OutsideTemperature", f_to_c(st.outside_temp))
            self._apply_aux("outside", "outside", INSTANCE_OFFSET_OUTSIDE,
                            st.outside_temp, TEMP_TYPE_OUTDOOR)
        elif not self._extra_has_oat:
            # No outside reading from either source.
            self._set(t, "/OutsideTemperature", None)
            if "outside" in self._aux:
                self._apply_aux("outside", "outside",
                                INSTANCE_OFFSET_OUTSIDE, None,
                                TEMP_TYPE_OUTDOOR)
        self._set(t, "/ModeName", st.mode_name)
        self._update_alarms(status=st)
        self._set(t, "/RunningState", st.running_state)
        self._set(t, "/Fault", st.fault_text)
        self._set(t, "/FaceplateTemperature", f_to_c(st.faceplate_temp))
        self._set(t, "/WeatherTemperature", f_to_c(st.weather_temp))
        if st.rev:
            self._set(t, "/FirmwareVersion", st.rev)
            self._set(self._switch, "/FirmwareVersion", st.rev)

        if not self._outputs_created:
            return

        sw = self._switch

        # While the user is mid-edit, don't fight the GUI: keep control
        # values as-is and only refresh measurements / status colours.
        if not self.edits_active:
            if not st.system_power or st.mode in (MODE_OFF, MODE_ZONE_OFF):
                self._set(sw, self._out("mode", "Dimming"), 0)   # "Off"
            elif st.mode in self._mode_values:
                self._set(sw, self._out("mode", "Dimming"),
                          self._mode_values.index(st.mode))

            self._update_fan_output(st)

            if self._out("cool", "Dimming") in sw:
                if st.mode in AUTO_MODES:
                    cool_f = st.auto_cool_sp
                elif st.mode == MODE_DRY:
                    cool_f = st.dry_sp
                else:
                    cool_f = st.cool_sp
                sw[self._out("cool", "Dimming")] = f_to_c_exact(cool_f)
            if self._out("heat", "Dimming") in sw:
                heat_f = st.auto_heat_sp if st.mode in AUTO_MODES else st.heat_sp
                sw[self._out("heat", "Dimming")] = f_to_c_exact(heat_f)

        self._set(sw, self._out("cool", "Measurement"), f_to_c(st.inside_temp))
        self._set(sw, self._out("heat", "Measurement"), f_to_c(st.inside_temp))

        # Status colours: power output reflects master power; the other
        # controls show Disabled while master power is off.
        # Mode stays interactive even when off (that is how you turn it on);
        # the rest grey out until the system is powered.
        self._set(sw, self._out("mode", "Status"), SW_STATUS_ON)
        dependent = SW_STATUS_ON if st.system_power else SW_STATUS_DISABLED
        for output in ("fan", "cool", "heat"):
            self._set(sw, self._out(output, "Status"), dependent)

    def _update_fan_output(self, st: ZoneStatus):
        """Rebuild the fan dropdown for the current mode and sync its value."""
        sw = self._switch
        labels_path = self._out("fan", "Settings/Labels")
        if labels_path not in sw or self.config is None:
            return

        mode = st.mode
        key = fan_change_key(mode)
        fa = self.config.fan_array
        opts = (fan_options(fa[mode])
                if key is not None and mode < len(fa) else [])

        if mode != self._fan_opts_mode or opts != self._fan_opts:
            self._fan_opts = opts
            self._fan_opts_mode = mode
            sw[labels_path] = [label for label, _v in opts]
            self._set(sw, self._out("fan", "Settings/ShowUIControl"),
                      1 if opts else 0)

        if opts:
            idx = fan_option_index(opts, st.fan_value())
            if idx is not None:
                self._set(sw, self._out("fan", "Dimming"), idx)

    # ── ExtraData diagnostics (GLib thread) ────────────────────────────

    def learn_ble_name(self, ble_name: str):
        """Learn the serial / A/C model prefix from the advertised name."""
        if self.model_prefix or not ble_name:
            return
        prefix = model_prefix_from_name(ble_name)
        if prefix:
            self.model_prefix = prefix
            logger.info("[%s] Detected A/C model prefix %s from BLE name %r",
                        self.dev_id, prefix, ble_name)

    def apply_extra(self, ed: ExtraData):
        t = self._temp
        self._set(t, "/Voltage", ed.vdc)
        self._set(self._switch, "/ModuleVoltage", ed.vdc)
        self._set(t, "/RoomSensorTemperature", f_to_c(ed.room_sensor))
        if ed.faceplate is not None:
            self._set(t, "/FaceplateTemperature", f_to_c(ed.faceplate))
        self._extra_has_oat = ed.outside_air is not None
        if ed.outside_air is not None:
            self._set(t, "/OutsideTemperature", f_to_c(ed.outside_air))
            self._apply_aux("outside", "outside", INSTANCE_OFFSET_OUTSIDE,
                            ed.outside_air, TEMP_TYPE_OUTDOOR)
        self._set(t, "/DiagnosticFlags",
                  ",".join(sorted(k for k, v in ed.flags.items() if v)))

        if ed.inside_coil is not None or "incoil" in self._aux:
            self._apply_aux("incoil", "in coil",
                            INSTANCE_OFFSET_IN_COIL, ed.inside_coil)
        if ed.outside_coil is not None or "outcoil" in self._aux:
            self._apply_aux("outcoil", "out coil",
                            INSTANCE_OFFSET_OUT_COIL, ed.outside_coil)

    def _create_freeze_input(self):
        """Register the virtual digital input that carries freeze alarms."""
        try:
            svc = VeDbusService(
                "com.victronenergy.digitalinput.easytouch_%s_freeze" % self.dev_id,
                new_private_bus(), register=False,
            )
            svc.add_path("/Mgmt/ProcessName", __file__)
            svc.add_path("/Mgmt/ProcessVersion", VERSION)
            svc.add_path("/Mgmt/Connection", "EasyTouch BLE %s" % self.mac)
            svc.add_path("/DeviceInstance", self.instance + INSTANCE_OFFSET_FREEZE)
            svc.add_path("/ProductId", DIGITAL_INPUT_PRODUCT_ID)
            svc.add_path("/ProductName", "EasyTouch freeze alarm")
            svc.add_path("/CustomName",
                         self.manager.custom_name(self.dev_id, "freeze",
                                                  "%s freeze" % self.name),
                         writeable=True,
                         onchangecallback=self._make_name_cb("freeze"))
            svc.add_path("/Connected", 1)
            svc.add_path("/Type", DIGITAL_INPUT_TYPE_GENERIC_IO)
            svc.add_path("/InputState", 0)
            svc.add_path("/State", 0)
            svc.add_path("/Alarm", DI_ALARM_OK)
            svc.add_path("/Count", 0)
            # Lets the alarm be silenced from the GUI without stopping the
            # driver, mirroring Victron's own digital-input setting.
            svc.add_path("/Settings/AlarmSetting", 1, writeable=True,
                         onchangecallback=lambda p, v: True)
            svc.add_path("/Settings/InvertAlarm", 0, writeable=True,
                         onchangecallback=lambda p, v: True)
            svc.add_path("/Settings/InvertTranslation", 0, writeable=True,
                         onchangecallback=lambda p, v: True)
            svc.register()
            self._freeze_input = svc
            self._freeze_count = 0
            logger.info("[%s] Registered freeze alarm input (instance %d)",
                        self.dev_id, self.instance + INSTANCE_OFFSET_FREEZE)
        except Exception:
            logger.exception("[%s] Could not register freeze alarm input",
                             self.dev_id)

    def _apply_freeze_input(self, active: bool):
        """Mirror the freeze condition onto the virtual digital input."""
        svc = self._freeze_input
        if svc is None:
            return
        enabled = bool(svc["/Settings/AlarmSetting"])
        alarm = DI_ALARM_ACTIVE if (active and enabled) else DI_ALARM_OK
        if svc["/InputState"] != int(active):
            if active:
                self._freeze_count += 1
                svc["/Count"] = self._freeze_count
            svc["/InputState"] = int(active)
            svc["/State"] = int(active)
        if svc["/Alarm"] != alarm:
            svc["/Alarm"] = alarm
            logger.info("[%s] Freeze input alarm -> %s", self.dev_id,
                        "ACTIVE" if alarm else "clear")

    def _update_alarms(self, status: ZoneStatus | None = None,
                       coil_f: int | None = None):
        """Raise/clear freeze and fault alarms (Venus: 0=ok, 1=warn, 2=alarm).

        Freezing is reported as a *warning* only: the unit shuts the
        compressor down and defrosts itself, so it should show up in
        notifications without raising an audible alarm.
        """
        if status is not None:
            self._last_fault = status.fault
        if coil_f is not None:
            self._last_coil_f = coil_f

        fault = getattr(self, "_last_fault", 0)

        low = 1 if fault == FAULT_FREEZE_DETECTED else 0
        self._apply_freeze_input(low != 0)

        if fault and fault != FAULT_FREEZE_DETECTED:
            other_fault = FAULT_SEVERITY.get(fault, 2)
        else:
            other_fault = 0

        t = self._temp
        if self._alarm_low != low:
            self._alarm_low = low
            self._set(t, "/Alarms/LowTemperature", low)
            logger.info("[%s] Freeze alarm -> %s", self.dev_id,
                        {0: "clear", 1: "warning", 2: "ALARM"}[low])
        if self._alarm_fault != other_fault:
            self._alarm_fault = other_fault
            self._set(t, "/Alarms/Fault", other_fault)
            if other_fault:
                logger.warning("[%s] Fault alarm: %s", self.dev_id,
                               fault_name(fault))
            else:
                logger.info("[%s] Fault alarm cleared", self.dev_id)

    def _apply_aux(self, kind: str, label: str, offset: int,
                   temp_f: int | None, temp_type: int = TEMP_TYPE_GENERIC):
        """Create (on first reading) and update an auxiliary temperature sensor."""
        svc = self._aux.get(kind)
        if svc is None:
            svc = VeDbusService(
                "com.victronenergy.temperature.easytouch_%s_%s" % (self.dev_id, kind),
                new_private_bus(), register=False,
            )
            svc.add_path("/Mgmt/ProcessName", __file__)
            svc.add_path("/Mgmt/ProcessVersion", VERSION)
            svc.add_path("/Mgmt/Connection", "EasyTouch BLE %s" % self.mac)
            svc.add_path("/DeviceInstance", self.instance + offset)
            svc.add_path("/ProductId", 0xFFFF)
            svc.add_path("/ProductName", "EasyTouch RV Thermostat")
            svc.add_path("/CustomName",
                         self.manager.custom_name(self.dev_id, kind,
                                                  "%s %s" % (self.name, label)),
                         writeable=True,
                         onchangecallback=self._make_name_cb(kind))
            svc.add_path("/Connected", 1)
            svc.add_path("/TemperatureType", temp_type)
            svc.add_path("/Temperature", None)
            svc.add_path("/Status", SENSOR_DISCONNECTED)
            svc.register()
            self._aux[kind] = svc
            logger.info("[%s] Registered %s sensor (instance %d)",
                        self.dev_id, label, self.instance + offset)
        svc["/Temperature"] = f_to_c(temp_f)
        svc["/Status"] = SENSOR_OK if temp_f is not None else SENSOR_DISCONNECTED

    def set_disconnected(self):
        self._temp["/Status"] = SENSOR_DISCONNECTED
        for svc in self._aux.values():
            svc["/Status"] = SENSOR_DISCONNECTED
        if self._outputs_created:
            for output in ("mode", "fan", "cool", "heat"):
                self._set(self._switch, self._out(output, "Status"),
                          SW_STATUS_DISABLED)


# ── Manager ───────────────────────────────────────────────────────────────────

class EasyTouchManager:
    """Loads configured thermostats, polls them, and dispatches control writes."""

    def __init__(self):
        self._config = load_config()
        defaults = self._config["DEFAULT"] if "DEFAULT" in self._config else {}

        # "intermittent" is the user-facing term; "transient" is accepted
        # as a synonym for configs written against earlier versions.
        _conn = defaults.get("connection", "persistent").strip().lower()
        self._persistent = _conn not in ("transient", "intermittent")
        self._poll_seconds = float(defaults.get(
            "poll_seconds", str(DEFAULT_POLL_SECONDS)))
        self._diagnostics_seconds = float(defaults.get(
            "diagnostics_seconds", str(DEFAULT_DIAGNOSTICS_SECONDS)))
        self._poll_interval_minutes = float(defaults.get(
            "poll_interval_minutes", str(DEFAULT_POLL_INTERVAL_MINUTES)))
        self._default_passcode = defaults.get("passcode", "").strip()
        self.freeze_input_enabled = defaults.get(
            "freeze_alarm_input", "true").strip().lower() not in (
                "0", "false", "no", "off")

        self._devices: dict[str, ThermostatDevice] = {}      # dev_id -> device
        self._next_instance = DEVICE_INSTANCE_BASE

        # localsettings persistence for GUI-adjustable monitor settings.
        # Unavailable (None) when running outside Venus OS.
        self._settings: SettingsDevice | None = None
        self._device_setting_items: dict[str, dict] = {}      # dev_id -> items
        try:
            self._settings = SettingsDevice(
                dbus.SystemBus() if "DBUS_SESSION_BUS_ADDRESS" not in os.environ
                else dbus.SessionBus(),
                {}, eventCallback=None, timeout=10,
            )
        except Exception:
            logger.warning("localsettings unavailable; GUI monitor settings "
                           "will not persist across restarts")

        # ── Persistent asyncio loop (see shyion rationale): a single long-
        # lived loop keeps bleak's per-loop BlueZManager / dbus_fast MessageBus
        # singleton alive so system-bus connections are not leaked per BLE op.
        # All device monitor tasks run on this loop.
        self._async_loop_ready = threading.Event()
        self._async_loop: asyncio.AbstractEventLoop | None = None
        self._async_thread = threading.Thread(
            target=self._run_async_loop, name="EasyTouch_AsyncLoop", daemon=True,
        )
        self._async_thread.start()
        if not self._async_loop_ready.wait(timeout=5.0):
            logger.error("Async event loop failed to start within 5 s")

        self._device_configs = get_device_configs(self._config)
        logger.info("Configured thermostats: %d", len(self._device_configs))
        if self._persistent:
            logger.info("Connection: persistent, status every %.0fs, "
                        "diagnostics every %.0fs",
                        self._poll_seconds, self._diagnostics_seconds)
        else:
            logger.info("Connection: transient, session every %.0f min",
                        self._poll_interval_minutes)

        self._load_configured_devices()
        self._start_monitors()

    # ── Device setup ───────────────────────────────────────────────────

    def _load_configured_devices(self):
        for mac, conf in self._device_configs.items():
            try:
                zone = int(conf.get("zone", "0"))
            except ValueError:
                zone = 0
            passcode = conf.get("passcode") or self._default_passcode
            self._add_device(mac, conf["name"], zone, passcode,
                             conf.get("serial", ""))

    def _add_device(self, mac: str, name: str, zone: int, passcode: str,
                    serial: str = "") -> ThermostatDevice | None:
        dev_id = mac.replace(":", "").lower() + ("_z%d" % zone if zone else "")
        if dev_id in self._devices:
            return self._devices[dev_id]
        instance = self._next_instance
        self._next_instance += INSTANCE_STRIDE
        monitor_settings = self._load_monitor_settings(dev_id)
        try:
            dev = ThermostatDevice(self, mac, name, zone, passcode, instance,
                                   serial, monitor_settings)
        except Exception:
            logger.exception("Failed to register thermostat %s", mac)
            return None
        self._devices[dev_id] = dev
        return dev

    # ── GUI-adjustable monitor settings ────────────────────────────────

    def _load_monitor_settings(self, dev_id: str) -> MonitorSettings:
        """Build a device's MonitorSettings: config defaults, then any
        values persisted in localsettings from earlier GUI adjustments."""
        default_persistent = 1 if self._persistent else 0
        default_poll = int(self._poll_seconds if self._persistent
                           else self._poll_interval_minutes * 60)
        default_poll = max(POLL_SECONDS_MIN, min(POLL_SECONDS_MAX, default_poll))

        ms = MonitorSettings(
            persistent=bool(default_persistent),
            poll_seconds=float(default_poll),
            diagnostics_seconds=self._diagnostics_seconds,
            reconnect_base=RECONNECT_BASE_DELAY,
            reconnect_max=RECONNECT_MAX_DELAY,
        )
        if self._settings is None:
            return ms
        try:
            base = "/Settings/Devices/easytouch_%s" % dev_id
            items = self._device_setting_items.setdefault(dev_id, {})
            items["Persistent"] = self._settings.addSetting(
                base + "/Persistent", default_persistent, 0, 1)
            v = items["Persistent"].get_value()
            if v is not None:
                ms.persistent = bool(int(v))
            # poll_seconds comes from config.ini only -- it is not a GUI
            # control, so nothing overrides the configured default.
        except Exception:
            logger.exception("[%s] Failed to load persisted monitor settings",
                             dev_id)
        return ms

    # ── Device names (persisted, GUI-renameable) ───────────────────────

    def custom_name(self, dev_id: str, kind: str, default: str) -> str:
        """The persisted display name for one of a device's services.

        Falls back to ``default`` (built from config.ini) the first time,
        or whenever localsettings is unavailable.
        """
        if self._settings is None:
            return default
        key = "Name_%s" % kind
        items = self._device_setting_items.setdefault(dev_id, {})
        if key not in items:
            try:
                items[key] = self._settings.addSetting(
                    "/Settings/Devices/easytouch_%s/%s" % (dev_id, key),
                    default, 0, 0)
            except Exception:
                logger.exception("[%s] Could not register name setting %s",
                                 dev_id, kind)
                return default
        try:
            v = items[key].get_value()
            if v:
                return str(v)
        except Exception:
            pass
        return default

    def set_custom_name(self, dev_id: str, kind: str, value: str):
        item = self._device_setting_items.get(dev_id, {}).get("Name_%s" % kind)
        if item is None:
            return
        try:
            item.set_value(value)
        except Exception:
            logger.exception("[%s] Failed to persist name for %s", dev_id, kind)

    def monitor_setting_changed(self, dev: ThermostatDevice, key: str, value: int):
        """Persist a GUI monitor-setting change and wake the monitor task."""
        items = self._device_setting_items.get(dev.dev_id, {})
        item = items.get(key)
        if item is not None:
            try:
                item.set_value(value)
            except Exception:
                logger.exception("[%s] Failed to persist %s", dev.dev_id, key)
        # An empty change batch wakes the monitor so the new cadence or
        # connection mode takes effect promptly.
        if dev.change_queue is not None and self._async_loop is not None:
            self._async_loop.call_soon_threadsafe(
                dev.change_queue.put_nowait, (time.monotonic(), {}))

    # ── Device monitors ────────────────────────────────────────────────

    def _start_monitors(self):
        """Start one long-lived monitor task per device on the asyncio loop."""
        for dev in self._devices.values():
            dev.change_queue = self._make_queue()
            if dev.change_queue is None:
                logger.error("[%s] Could not create change queue; "
                             "device not monitored", dev.dev_id)
                continue
            dev.monitor_future = asyncio.run_coroutine_threadsafe(
                run_device_monitor(
                    dev.mac, dev.zone, dev.passcode, dev.model_prefix,
                    change_queue=dev.change_queue,
                    on_snapshot=self._make_snapshot_handler(dev.dev_id),
                    settings=dev.monitor_settings,
                ),
                self._async_loop,
            )
            logger.info("[%s] Monitor started (%s, %.0fs)", dev.dev_id,
                        "persistent" if dev.monitor_settings.persistent
                        else "transient",
                        dev.monitor_settings.poll_seconds)

    def _make_queue(self):
        """Create an asyncio.Queue bound to the monitor loop (Py3.8-safe)."""
        async def make():
            return asyncio.Queue()
        try:
            return asyncio.run_coroutine_threadsafe(
                make(), self._async_loop).result(timeout=5)
        except Exception:
            logger.exception("Failed to create change queue")
            return None

    def _make_snapshot_handler(self, dev_id: str):
        """Snapshot callback for a monitor; marshals onto the GLib thread."""
        def handler(snap: DeviceSnapshot):
            GLib.idle_add(self._apply_result, dev_id, snap)
        return handler

    def enqueue_change(self, dev: ThermostatDevice, changes: dict):
        """Queue a coalesced change batch for the device's monitor task."""
        if dev.change_queue is None or self._async_loop is None:
            logger.warning("[%s] No monitor; change dropped: %r",
                           dev.dev_id, changes)
            dev.change_finished()
            return
        self._async_loop.call_soon_threadsafe(
            dev.change_queue.put_nowait, (time.monotonic(), changes))

    def _apply_result(self, dev_id: str, snap: DeviceSnapshot) -> bool:
        dev = self._devices.get(dev_id)
        if dev is None:
            return False
        if snap.applied_changes:
            dev.change_finished(snap.applied_changes)
        if snap.ble_name:
            dev.learn_ble_name(snap.ble_name)
        if snap.config is not None:
            try:
                dev.apply_config(snap.config)
            except Exception:
                logger.exception("[%s] Failed to apply config", dev_id)
        if snap.status is not None:
            try:
                dev.apply_status(snap.status)
            except Exception:
                logger.exception("[%s] Failed to apply status", dev_id)
        else:
            dev.set_disconnected()
        if snap.extra is not None:
            try:
                dev.apply_extra(snap.extra)
            except Exception:
                logger.exception("[%s] Failed to apply diagnostics", dev_id)
        return False

    # ── Persistent asyncio loop ────────────────────────────────────────

    def _run_async_loop(self):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._async_loop = loop
        try:
            self._async_loop_ready.set()
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                logger.exception("Error closing async event loop")

    # ── Shutdown ───────────────────────────────────────────────────────

    def stop(self):
        # Cancel monitor tasks so their sessions disconnect cleanly.
        futures = [dev.monitor_future for dev in self._devices.values()
                   if dev.monitor_future is not None]
        for fut in futures:
            fut.cancel()
        for fut in futures:
            try:
                fut.result(timeout=6)
            except (concurrent.futures.CancelledError, asyncio.CancelledError):
                pass
            except Exception:
                pass
        loop = self._async_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        logger.info("Service stopped")


def main():
    DBusGMainLoop(set_as_default=True)
    manager = EasyTouchManager()
    mainloop = GLib.MainLoop()

    def signal_handler(signum, frame):
        logger.info("Received signal %d, shutting down...", signum)
        manager.stop()
        mainloop.quit()

    signal.signal(signal.SIGTERM, signal_handler)
    signal.signal(signal.SIGINT, signal_handler)

    logger.info("dbus-easytouchrv v%s started", VERSION)
    mainloop.run()


if __name__ == "__main__":
    main()
