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
entry point calls install_catcher_from_config() between setting up sys.path
and importing the BLE module.  Reading the adapter configuration here keeps
the daemon and the CLI tools (provision.py, easytouch_device.py) routing
identically: an `adapters = hci1` allowlist written for the service also
steers a hand-run scan off a dead onboard adapter.
"""

from __future__ import annotations

import configparser
import os


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


def install_catcher_from_config(owner: str) -> None:
    """Install the bleak catcher with the configured adapter routing."""
    from bleak_connection_manager import install_bleak_catcher

    adapters, link_caps = catcher_options()
    install_bleak_catcher(
        owner,
        adapters=adapters,
        link_caps=link_caps,
        wrap_scanner=True,
    )
