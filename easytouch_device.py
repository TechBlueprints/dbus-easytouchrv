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
Standalone BLE probe for a single EasyTouch RV thermostat.

Use this script to test BLE connectivity and thermostat control from the
command line, independent of the D-Bus service.

Usage:
    python3 easytouch_device.py --scan
    python3 easytouch_device.py --mac AA:BB:CC:DD:EE:FF --status
    python3 easytouch_device.py --mac AA:BB:CC:DD:EE:FF --mode cool --cool-sp 74
    python3 easytouch_device.py --mac AA:BB:CC:DD:EE:FF --power off
"""

import argparse
import asyncio
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("easytouch-device")

# BLE dependencies are placed on sys.path by dbus-easytouchrv.py; when running
# this probe standalone, ensure the ext/ folders are importable.
import os
import sys

_ext = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext")
for _sub in [
    os.path.join(_ext, "bleak-connection-manager", "src"),
    os.path.join(_ext, "bleak-retry-connector", "src"),
    os.path.join(_ext, "bluetooth-adapters", "src"),
    os.path.join(_ext, "aiooui", "src"),
    os.path.join(_ext, "bleak"),
]:
    if os.path.isdir(_sub) and _sub not in sys.path:
        sys.path.insert(0, _sub)

# Installed before easytouch_ble captures bleak's classes at import time.
# On non-Linux hosts the catcher's claims/mgmt layers degrade to no-ops.
from bleak_connection_manager import install_bleak_catcher  # noqa: E402

install_bleak_catcher("easytouch-probe", wrap_scanner=True)

from easytouch_ble import (  # noqa: E402
    EasyTouchSession,
    scan_for_thermostats,
    model_prefix_from_name,
    MODE_NAMES,
)

_MODE_BY_NAME = {v.lower().replace(" ", ""): k for k, v in MODE_NAMES.items()}
_MODE_BY_NAME.update({"heat": 3, "furnace": 4, "auto": 8, "fan": 1})


def _print_status(st) -> None:
    if st is None:
        print("No status received")
        return
    inside = "%d F" % st.inside_temp if st.inside_temp is not None else "?"
    outside = "%d F" % st.outside_temp if st.outside_temp is not None else "--"
    print("Zone %d" % st.zone)
    print("  Mode        : %s" % st.mode_name)
    print("  Inside temp : %s" % inside)
    print("  Outside temp: %s" % outside)
    print("  Cool SP     : %d F" % st.cool_sp)
    print("  Heat SP     : %d F" % st.heat_sp)
    print("  Running     : %s" % st.running_state)
    print("  System power: %s" % ("on" if st.system_power else "off"))
    if st.fault_text:
        print("  FAULT       : %s" % st.fault_text)


async def probe(args) -> None:
    passcode = args.passcode or ""
    async with EasyTouchSession(args.mac, passcode=passcode) as s:
        changes: dict[str, int] = {}

        if args.power:
            await s.set_power(args.zone, args.power == "on")

        if args.mode:
            mode = _MODE_BY_NAME.get(args.mode.lower().replace(" ", ""))
            if mode is None:
                print("Unknown mode: %s (choices: %s)" %
                      (args.mode, ", ".join(sorted(set(MODE_NAMES.values())))))
            else:
                changes["mode"] = mode

        if args.cool_sp is not None:
            changes["cool_sp"] = args.cool_sp
        if args.heat_sp is not None:
            changes["heat_sp"] = args.heat_sp

        if changes:
            ok = await s.change(args.zone, **changes)
            print("Change %s: %s" % (changes, "OK" if ok else "not acknowledged"))
            await asyncio.sleep(0.4)

        st = await s.get_status(args.zone)
        _print_status(st)

        if args.extra:
            prefix = args.serial[:3] if args.serial else model_prefix_from_name(s.ble_name)
            ed = await s.get_extra_data(args.zone, prefix)
            if ed is None:
                print("No ExtraData received")
            else:
                print("ExtraData (model prefix %s):" % (prefix or "unknown"))
                for label, value in (
                    ("Outside air", ed.outside_air),
                    ("Inside coil", ed.inside_coil),
                    ("Outside coil", ed.outside_coil),
                    ("Faceplate", ed.faceplate),
                    ("Room sensor", ed.room_sensor),
                ):
                    if value is not None:
                        print("  %-12s: %d F" % (label, value))
                if ed.vdc is not None:
                    print("  %-12s: %.2f V" % ("Supply", ed.vdc))
                active = sorted(k for k, v in ed.flags.items() if v)
                if active:
                    print("  Flags       : %s" % ", ".join(active))
                print("  Raw         : %s" % ed.raw)


async def scan_only() -> None:
    found = await scan_for_thermostats(timeout=12.0)
    if found:
        print("\nDiscovered %d EasyTouch device(s):" % len(found))
        for d in found:
            print("  %s  %s  serial=%s  RSSI=%d" % (d.mac, d.name, d.serial, d.rssi))
    else:
        print("\nNo EasyTouch devices found")


def main():
    parser = argparse.ArgumentParser(description="EasyTouch RV thermostat probe")
    parser.add_argument("--mac", help="BLE MAC address of the thermostat")
    parser.add_argument("--zone", type=int, default=0, help="Zone index (default 0)")
    parser.add_argument("--passcode", default="", help="Account passcode (if required)")
    parser.add_argument("--scan", action="store_true", help="Scan for EasyTouch devices only")
    parser.add_argument("--status", action="store_true", help="Read status (default action)")
    parser.add_argument("--mode", help="Set mode (cool, heat, auto, fan, off, ...)")
    parser.add_argument("--power", choices=["on", "off"], help="Set master power")
    parser.add_argument("--cool-sp", type=int, dest="cool_sp", help="Set cool setpoint (F)")
    parser.add_argument("--heat-sp", type=int, dest="heat_sp", help="Set heat setpoint (F)")
    parser.add_argument("--extra", action="store_true",
                        help="Also read model-specific diagnostics (coil temps, flags)")
    parser.add_argument("--serial", default="",
                        help="9-digit serial (for the diagnostics decoder; "
                             "auto-detected from the BLE name when omitted)")
    args = parser.parse_args()

    if args.scan:
        asyncio.run(scan_only())
        return

    if not args.mac:
        parser.error("--mac is required (or use --scan)")

    asyncio.run(probe(args))


if __name__ == "__main__":
    main()
