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

BLE stack resolution: bleak-connection-manager is NEVER vendored here.  It
is installed once per box at /data/bcm (install.sh step 2b) and shared by
every consumer; the service/run script execs its interpreter shim
(/data/bcm/python3), which serves bleak, bleak-retry-connector,
bleak-connection-manager, dbus-fast and friends.  This repo vendors only
the rest of the stack, as a fallback for a box that has no /data/bcm.

Consequence on Venus specifically: /data/bcm is also where dbus-fast 5
comes from, and Venus ships 2.21.1 while bleak needs >= 4.  So on Venus
the fallback cannot satisfy bleak either, and install.sh is required
rather than merely recommended.  _ensure_ble_stack() says so plainly
instead of letting it surface as an ImportError deep inside bleak.
"""

from __future__ import annotations

import configparser
import logging
import os
import sys

logger = logging.getLogger(__name__)


def _warn_if_dbus_fast_too_old() -> None:
    """Say plainly when the fallback cannot actually satisfy bleak.

    bleak needs dbus-fast >= 4; Venus ships 2.21.1, whose package has no
    ``annotations`` module.  /data/bcm is what supplies a new enough copy,
    so without it the very next import of bleak dies somewhere inside
    dbus_fast with nothing pointing at the cause.  One line here costs
    nothing and turns that into an actionable message.
    """
    try:
        import importlib.util
        if importlib.util.find_spec("dbus_fast") is None:
            return                      # not Linux/BlueZ, or no dbus stack
        if importlib.util.find_spec("dbus_fast.annotations") is not None:
            return                      # new enough
    except (ImportError, ValueError):
        return
    logger.error(
        "dbus-fast is too old for bleak (no dbus_fast.annotations; bleak "
        "needs >= 4, Venus ships 2.21.1). The shared install at /data/bcm "
        "supplies a current one -- run install.sh. Importing bleak will "
        "fail until then.")


def _ensure_ble_stack() -> None:
    """Put the vendored ext/ BLE stack on sys.path unless already provided
    (by the shared /data/bcm shim, or by test stubs).

    bleak-connection-manager is deliberately NOT among the vendored
    entries: it lives at /data/bcm and nowhere else (see module docstring).
    """
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
        os.path.join(_ext, "bleak-retry-connector", "src"),
        os.path.join(_ext, "bluetooth-adapters", "src"),
        os.path.join(_ext, "aiooui", "src"),
        os.path.join(_ext, "bleak"),
    ]:
        if os.path.isdir(_sub) and _sub not in sys.path:
            sys.path.insert(0, _sub)
    _warn_if_dbus_fast_too_old()


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


def install_catcher_from_config(owner: str) -> bool:
    """Install the bleak catcher with the configured adapter routing.

    Returns whether the catcher was installed.  BCM is optional: a checkout
    cloned without --recursive, or a box with only bleak and
    bleak-retry-connector, still runs.  What is lost is stated plainly at
    startup rather than failing the service, because the driver's own
    retry/backoff and post-connect validation (easytouch_ble) do not depend
    on it -- only the multi-adapter routing does.
    """
    _ensure_ble_stack()
    try:
        from bleak_connection_manager import install_bleak_catcher
    except ImportError as exc:
        adapters, link_caps = catcher_options()
        logger.warning(
            "bleak-connection-manager not available (%s); running on plain "
            "bleak-retry-connector. Lost: claim-aware adapter selection, "
            "cross-process link coordination, per-adapter link caps, and "
            "connection-parameter tuning. BlueZ picks the adapter.", exc)
        # Silence is the wrong answer when the operator configured routing
        # that is now being ignored: an "MAC@hciX" pin reads as honoured.
        if adapters:
            logger.warning(
                "adapters = %s is NOT being applied without "
                "bleak-connection-manager", " ".join(adapters))
        if link_caps:
            logger.warning(
                "link_caps = %s is NOT being applied without "
                "bleak-connection-manager",
                " ".join("%s:%d" % kv for kv in sorted(link_caps.items())))
        return False

    adapters, link_caps = catcher_options()
    install_bleak_catcher(
        owner,
        adapters=adapters,
        link_caps=link_caps,
        wrap_scanner=True,
        tune_conn_params=_tune_conn_params(),
    )
    return True
