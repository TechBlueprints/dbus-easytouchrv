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

Implements the fleet consumer contract (bleak-connection-manager's
CONSUMERS.md).  Two things follow from it and are easy to undo by accident:

  * The BCM location is CONFIGURATION, not launch environment.  One key,
    bluetooth_connection_manager_dir (default /data/bcm, empty = never
    look).  Nothing about how the process was started decides where the
    stack comes from -- no interpreter shim, no PYTHONPATH, no env var.
    The run script therefore execs a plain python3.
  * ensure_ble_stack() is called UNCONDITIONALLY and FIRST, before
    anything imports bleak.  Two reasons: easytouch_ble captures
    BleakClient/BleakScanner at import time, and the sitewide autowire
    hook stands down only for a process that already has
    bleak_connection_manager in sys.modules.  A late call means autowire
    installs a generic catcher with a cmdline-derived owner and no pins.

Reading the adapter configuration here also keeps the daemon and the CLI
tools (provision.py, easytouch_device.py) routing identically: an
`adapters` allowlist written for the service also steers a hand-run scan
off a dead onboard adapter.

BCM itself is never vendored (CONSUMERS.md section 3).  This repo vendors
only bleak, bleak-retry-connector, bluetooth-adapters and aiooui, and
those roots are inserted ONLY once ble_stack has reported "vendored".
On that path nothing is imported from bleak_connection_manager and no
BCM-only adapter syntax (MAC@hciX, MAC-keyed pins) reaches plain bleak --
the routing is simply not applied, and said so at startup.

Venus note: /data/bcm is also where dbus-fast 5 comes from, and Venus
ships 2.21.1 while bleak needs >= 4.  So on Venus the vendored fallback
cannot satisfy bleak either and install.sh is required, not optional;
_warn_if_dbus_fast_too_old() says so rather than letting it surface as an
ImportError inside dbus_fast.
"""

from __future__ import annotations

import configparser
import inspect
import logging
import os
import sys

import ble_stack
from ble_stack import ensure_ble_stack

logger = logging.getLogger(__name__)

# The shared install gained force_start_notify= at 159536a (2026-09-02).
_LEGACY_START_NOTIFY_ENV = "BCM_FORCE_START_NOTIFY"

_CONFIG_NAMES = ("config.ini", "config.default.ini")


def _config() -> configparser.SectionProxy | dict:
    cfg = configparser.ConfigParser()
    config_dir = os.path.dirname(os.path.abspath(__file__))
    for name in _CONFIG_NAMES:
        path = os.path.join(config_dir, name)
        if os.path.exists(path):
            cfg.read(path)
            break
    return cfg["DEFAULT"] if "DEFAULT" in cfg else {}


def _bool(value: str, default: bool) -> bool:
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


def shared_dir() -> str:
    """Configured BCM location.  Empty string means never look."""
    raw = _config().get("bluetooth_connection_manager_dir", "/data/bcm")
    return str(raw).strip()


def force_start_notify() -> bool:
    """Per-process StartNotify policy, passed to install_bleak_catcher."""
    return _bool(
        _config().get(
            "bluetooth_connection_manager_force_start_notify", "true"),
        True)


def _vendored_roots() -> list[str]:
    """This repo's standalone BLE stack.  Deliberately NOT a BCM copy."""
    ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
    return [
        os.path.join(ext, "bleak-retry-connector", "src"),
        os.path.join(ext, "bluetooth-adapters", "src"),
        os.path.join(ext, "aiooui", "src"),
        os.path.join(ext, "bleak"),
    ]


def _warn_if_dbus_fast_too_old() -> None:
    """Say plainly when the vendored fallback cannot actually satisfy bleak.

    bleak needs dbus-fast >= 4; Venus ships 2.21.1, whose package has no
    ``annotations`` module.  Without /data/bcm the next import of bleak
    dies inside dbus_fast with nothing pointing at the cause.
    """
    try:
        import importlib.util
        if importlib.util.find_spec("dbus_fast") is None:
            return                      # no dbus stack at all; not our case
        if importlib.util.find_spec("dbus_fast.annotations") is not None:
            return                      # new enough
    except (ImportError, ValueError):
        return
    logger.error(
        "dbus-fast is too old for bleak (no dbus_fast.annotations; bleak "
        "needs >= 4, Venus ships 2.21.1). The shared install supplies a "
        "current one -- run install.sh. Importing bleak will fail until "
        "then.")


def catcher_options() -> tuple[list[str], dict[str, int]]:
    """Adapter entries and link caps for the bleak catcher, from config.ini."""
    defaults = _config()
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
    return _bool(_config().get("tune_conn_params", "false"), False)


def _report_uncoordinated(directory: str) -> None:
    """The vendored path: insert our own roots and say what is lost.

    Silence here would be the worst outcome: a configured `adapters` pin
    reads as honoured while BlueZ picks the card.
    """
    logger.warning(
        "BLE coordination: no shared install at %s; running uncoordinated, "
        "no claims, no adapter routing, no card recovery", directory)
    for root in _vendored_roots():
        if os.path.isdir(root) and root not in sys.path:
            sys.path.insert(1, root)
    _warn_if_dbus_fast_too_old()
    adapters, link_caps = catcher_options()
    if adapters:
        logger.warning(
            "adapters = %s is NOT being applied without the shared install",
            " ".join(adapters))
    if link_caps:
        logger.warning(
            "link_caps = %s is NOT being applied without the shared install",
            " ".join("%s:%d" % kv for kv in sorted(link_caps.items())))


def install_catcher_from_config(owner: str) -> bool:
    """Source the shared BCM install and install the catcher.

    Returns whether the catcher was installed.  Every failure is a warning
    or an error and never an exception: the driver runs uncoordinated on
    plain bleak-retry-connector rather than not at all.
    """
    directory = shared_dir()
    state = ensure_ble_stack(shared_dir=directory, vendored_dir=None)

    if state == "vendored":
        # ble_stack withdraws every path and module a broken install
        # contributed before returning, so this is safe either way.  The
        # two cases are different operator actions and never logged alike.
        # shared_failure is set on the module by ensure_ble_stack, so it
        # must be read off the module object, not bound at import time.
        if ble_stack.shared_failure:
            logger.error(
                "BLE coordination: shared install at %s is present but "
                "unusable, running uncoordinated: %s",
                directory, ble_stack.shared_failure)
            for root in _vendored_roots():
                if os.path.isdir(root) and root not in sys.path:
                    sys.path.insert(1, root)
            _warn_if_dbus_fast_too_old()
        else:
            _report_uncoordinated(directory)
        return False

    import bleak_connection_manager
    from bleak_connection_manager import install_bleak_catcher

    # The package's OWN directory, not the configured key: this line has to
    # prove which tree actually served.  A namespace package has no
    # __file__, so fall back rather than dying on the diagnostic.
    package_file = getattr(bleak_connection_manager, "__file__", None)
    loaded_from = (os.path.dirname(os.path.abspath(package_file))
                   if package_file
                   else os.path.join(directory, "src",
                                     "bleak_connection_manager"))
    logger.info(
        "BLE coordination: bleak_connection_manager loaded from %s",
        loaded_from)

    adapters, link_caps = catcher_options()
    notify = force_start_notify()
    kwargs = dict(adapters=adapters, link_caps=link_caps, wrap_scanner=True,
                  tune_conn_params=_tune_conn_params())
    try:
        supported = "force_start_notify" in inspect.signature(
            install_bleak_catcher).parameters
    except (TypeError, ValueError):
        supported = False
    if supported:
        kwargs["force_start_notify"] = notify
    else:
        # Pre-159536a install: the parameter does not exist yet, so the
        # policy can only travel through the legacy environment read.
        os.environ[_LEGACY_START_NOTIFY_ENV] = "true" if notify else "false"
        logger.warning(
            "BLE coordination: shared install at %s predates the "
            "force_start_notify parameter; StartNotify policy passed "
            "through the legacy BCM_FORCE_START_NOTIFY environment",
            directory)

    try:
        install_bleak_catcher(owner, **kwargs)
    except Exception as exc:
        # Distinct from "present but unusable": the install is fine, the
        # driver or the catcher is not, and that is a different fix.
        logger.error(
            "BLE coordination: catcher would not install from %s, running "
            "uncoordinated: %r", directory, exc)
        return False

    logger.info(
        "BLE coordination: catcher installed (force_start_notify=%s, "
        "adapters=%d configured, %d pinned)",
        notify, len(adapters), sum(1 for a in adapters if "@" in a))
    return True
