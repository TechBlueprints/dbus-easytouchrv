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

"""Shared bleak-catcher installation for every entry point.

The catcher must be installed before easytouch_ble is imported (that module
captures bleak.BleakClient / bleak.BleakScanner at import time), so each
entry point calls install_catcher_from_config() before importing the BLE
module.  Reading the adapter configuration here keeps the daemon and the
CLI tools (provision.py, easytouch_device.py) routing identically: an
`adapters = hci1` allowlist written for the service also steers a hand-run
scan off a dead onboard adapter.

BLE stack resolution: on a fleet install the service/run script execs the
shared BCM checkout's interpreter shim (/data/bcm/python3), which serves
bleak, bleak-retry-connector, bleak-connection-manager, dbus-fast and
friends for every consumer on the box.  A standalone checkout (bare clone,
plain python3) gets the same stack from this repo's vendored ext/
submodules instead.  _ensure_ble_stack() prefers whatever the interpreter
already provides and only falls back to ext/.
"""

from __future__ import annotations

import configparser
import os
import sys


def _ensure_ble_stack() -> None:
    """Put the vendored ext/ BLE stack on sys.path unless already provided
    (by the shared /data/bcm shim, or by test stubs)."""
    if "bleak_connection_manager" in sys.modules:
        return
    try:
        import importlib.util
        if importlib.util.find_spec("bleak_connection_manager") is not None:
            return
    except (ImportError, ValueError):
        pass
    _ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
    for _sub in [
        os.path.join(_ext, "bleak-connection-manager", "src"),
        os.path.join(_ext, "bleak-connection-manager", "ext"),
        os.path.join(_ext, "bleak-retry-connector", "src"),
        os.path.join(_ext, "bluetooth-adapters", "src"),
        os.path.join(_ext, "aiooui", "src"),
        os.path.join(_ext, "bleak"),
    ]:
        if os.path.isdir(_sub) and _sub not in sys.path:
            sys.path.insert(0, _sub)


def catcher_options() -> tuple[list[str], dict[str, int]]:
    """Adapter entries and link caps for the bleak catcher, from config.ini."""
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


def _tune_conn_params() -> bool:
    """Whether to let the catcher tune BLE connection parameters.

    Default OFF for this driver.  The catcher's habluetooth-derived tuning
    loads a 7.5 ms connect interval, relaxing to 8.75-11.25 ms with an 8 s
    supervision timeout.  That is tuned for battery BMS links; an EasyTouch
    thermostat is a low-rate device polled every 10 s, so the aggressive
    interval buys nothing, while the short supervision timeout drops the
    link on any marginal RF moment.  Enable with `tune_conn_params = true`
    in config.ini if a deployment wants it.
    """
    cfg = configparser.ConfigParser()
    config_dir = os.path.dirname(os.path.abspath(__file__))
    for name in ("config.ini", "config.default.ini"):
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            cfg.read(path)
            break
    defaults = cfg["DEFAULT"] if "DEFAULT" in cfg else {}
    return str(defaults.get("tune_conn_params", "false")).strip().lower() in (
        "1", "true", "yes", "on")


def install_catcher_from_config(owner: str) -> None:
    """Install the bleak catcher with the configured adapter routing."""
    _ensure_ble_stack()
    from bleak_connection_manager import install_bleak_catcher

    adapters, link_caps = catcher_options()
    install_bleak_catcher(
        owner,
        adapters=adapters,
        link_caps=link_caps,
        wrap_scanner=True,
        tune_conn_params=_tune_conn_params(),
    )
