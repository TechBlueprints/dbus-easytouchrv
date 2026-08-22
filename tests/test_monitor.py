"""Tests for the device monitor: change draining/merging/expiry and the
supervisor loop, driven with a fake BLE session (no radio, no D-Bus)."""
import asyncio
import os
import sys
import types
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

for name in ("bleak",):
    mod = types.ModuleType(name)
    mod.BleakClient = object
    mod.BleakScanner = object
    sys.modules.setdefault(name, mod)

brc = types.ModuleType("bleak_retry_connector")
brc.establish_connection = lambda *a, **k: None
sys.modules.setdefault("bleak_retry_connector", brc)

bcm = types.ModuleType("bleak_connection_manager")
bcm.install_bleak_catcher = lambda *a, **k: None
vmod = types.ModuleType("bleak_connection_manager.validators")
vmod.validate_char_exists = lambda uuid: (lambda client: True)
vmod.tolerate_late_gatt = lambda validator: validator
bcm.validators = vmod
sys.modules.setdefault("bleak_connection_manager", bcm)
sys.modules.setdefault("bleak_connection_manager.validators", vmod)

sys.path.insert(0, _REPO_ROOT)

import easytouch_ble as et  # noqa: E402


STATUS_OBJ = {
    "Type": "Response", "RT": "Status", "REV": "1.0.6.5",
    "PRM": [0, 8, 78, 72],
    "Z_sts": {"0": [70, 76, 74, 68, 72, 50, 1, 1, 2, 1, 2, 3, 73, 88, 0, 2]},
}


class FakeSession:
    """Stands in for EasyTouchSession; records commands, serves canned data.

    Class attributes configure behaviour for the next instantiation.
    """
    instances = []
    fail_connects = 0          # raise on __aenter__ this many times
    status_replies = None      # optional list; None entry = no reply

    def __init__(self, address, passcode=""):
        self.address = address
        self.ble_name = "EasyTouch 347000123"
        self.commands = []
        self.entered = False
        self.exited = False
        FakeSession.instances.append(self)

    async def __aenter__(self):
        if FakeSession.fail_connects > 0:
            FakeSession.fail_connects -= 1
            raise ConnectionError("simulated connect failure")
        self.entered = True
        return self

    async def __aexit__(self, *exc):
        self.exited = True

    async def get_config(self, zone):
        self.commands.append(("config", zone))
        cfg = et.ZoneConfig(zone=zone)
        cfg.available_modes = 0x7
        cfg.fan_array = [0] * 16
        return cfg

    async def get_status(self, zone):
        self.commands.append(("status", zone))
        if FakeSession.status_replies:
            reply = FakeSession.status_replies.pop(0)
            if reply is None:
                return None
        return et._parse_status(STATUS_OBJ)[0]

    async def get_extra_data(self, zone, prefix):
        self.commands.append(("extra", zone, prefix))
        return et.parse_extra_data({"InCoil": 55}, prefix)

    async def change(self, zone, **changes):
        self.commands.append(("change", zone, changes))
        return True


def reset_fake():
    FakeSession.instances = []
    FakeSession.fail_connects = 0
    FakeSession.status_replies = None


async def run_monitor_cycles(snapshots, n_cycles, **kw):
    """Run the monitor until n_cycles snapshots arrive, then cancel."""
    done = asyncio.Event()

    def on_snapshot(snap):
        snapshots.append(snap)
        if len(snapshots) >= n_cycles:
            done.set()

    queue = asyncio.Queue()
    settings = kw.pop("settings", None) or et.MonitorSettings(
        persistent=kw.pop("persistent", True),
        poll_seconds=kw.pop("poll_seconds", 0.01),
        diagnostics_seconds=kw.pop("diagnostics_seconds", 0.0),
        reconnect_base=0.01,
        reconnect_max=0.02,
    )
    task = asyncio.ensure_future(et.run_device_monitor(
        "AA:BB:CC:DD:EE:FF", 0, "", "",
        change_queue=kw.pop("change_queue", queue),
        on_snapshot=on_snapshot,
        session_factory=FakeSession,
        settings=settings,
        **kw,
    ))
    try:
        await asyncio.wait_for(done.wait(), timeout=5)
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
    return queue


class DrainChangesTests(unittest.TestCase):
    def drain(self, items, max_age=120.0, now=1000.0):
        async def go():
            q = asyncio.Queue()
            for item in items:
                q.put_nowait(item)
            return et._drain_changes(q, [], max_age=max_age, now=now)
        return asyncio.get_event_loop().run_until_complete(go())

    def test_merge_order(self):
        merged, consumed, dropped = self.drain([
            (999.0, {"mode": 2, "cool_sp": 74}),
            (999.5, {"cool_sp": 76}),
        ])
        self.assertEqual(merged, {"mode": 2, "cool_sp": 76})
        self.assertEqual(consumed, 2)
        self.assertEqual(dropped, 0)

    def test_expiry(self):
        merged, consumed, dropped = self.drain([
            (800.0, {"power": 0}),     # 200s old -> dropped
            (999.0, {"mode": 2}),
        ])
        self.assertEqual(merged, {"mode": 2})
        self.assertEqual(consumed, 2)
        self.assertEqual(dropped, 1)

    def test_pending_carried(self):
        async def go():
            q = asyncio.Queue()
            q.put_nowait((999.0, {"heat_sp": 70}))
            pending = [(998.0, {"mode": 4})]
            merged, consumed, _ = et._drain_changes(q, pending, now=1000.0)
            return merged, consumed, pending
        merged, consumed, pending = asyncio.get_event_loop().run_until_complete(go())
        self.assertEqual(merged, {"mode": 4, "heat_sp": 70})
        self.assertEqual(consumed, 2)
        self.assertEqual(pending, [])


class MonitorTests(unittest.TestCase):
    def setUp(self):
        reset_fake()

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_persistent_polls_and_config_once(self):
        snapshots = []
        self.run_async(run_monitor_cycles(snapshots, 3))
        self.assertEqual(len(FakeSession.instances), 1)  # one connection
        s = FakeSession.instances[0]
        self.assertEqual([c for c in s.commands if c[0] == "config"],
                         [("config", 0)])
        self.assertGreaterEqual(
            len([c for c in s.commands if c[0] == "status"]), 3)
        self.assertIsNotNone(snapshots[0].config)
        self.assertIsNone(snapshots[1].config)
        self.assertEqual(snapshots[0].ble_name, "EasyTouch 347000123")

    def test_diagnostics_cadence(self):
        snapshots = []
        # diagnostics_seconds=0 -> every cycle
        self.run_async(run_monitor_cycles(snapshots, 2))
        self.assertIsNotNone(snapshots[0].extra)
        # prefix derived from the BLE name
        s = FakeSession.instances[0]
        extra_cmds = [c for c in s.commands if c[0] == "extra"]
        self.assertEqual(extra_cmds[0][2], "347")

    def test_change_applied_and_reported(self):
        snapshots = []

        async def go():
            queue = asyncio.Queue()
            queue.put_nowait((et.time.monotonic(), {"mode": 2}))
            queue.put_nowait((et.time.monotonic(), {"cool_sp": 75}))
            await run_monitor_cycles(snapshots, 1, change_queue=queue)
        self.run_async(go())
        s = FakeSession.instances[0]
        changes = [c for c in s.commands if c[0] == "change"]
        self.assertEqual(changes, [("change", 0, {"mode": 2, "cool_sp": 75})])
        self.assertEqual(snapshots[0].applied_changes, 2)

    def test_reconnect_after_connect_failure(self):
        snapshots = []
        FakeSession.fail_connects = 2
        self.run_async(run_monitor_cycles(snapshots, 3))
        # two failure snapshots (status None), then a good one
        self.assertIsNone(snapshots[0].status)
        self.assertIsNone(snapshots[1].status)
        self.assertIsNotNone(snapshots[2].status)
        # 2 failed factories + 1 good
        self.assertEqual(len(FakeSession.instances), 3)

    def test_missed_status_triggers_reconnect(self):
        snapshots = []
        # First connection: three unanswered polls -> teardown snapshot,
        # then a fresh connection answers.  5 snapshots total.
        FakeSession.status_replies = [None, None, None]
        self.run_async(run_monitor_cycles(snapshots, 5))
        self.assertEqual(len(FakeSession.instances), 2)
        self.assertTrue(FakeSession.instances[0].exited)  # clean disconnect
        self.assertIsNone(snapshots[0].status)
        self.assertIsNotNone(snapshots[-1].status)

    def test_transient_disconnects_between_cycles(self):
        snapshots = []
        self.run_async(run_monitor_cycles(
            snapshots, 2, persistent=False))
        self.assertGreaterEqual(len(FakeSession.instances), 2)
        self.assertTrue(all(s.exited for s in FakeSession.instances
                            if s.entered))

    def test_live_settings_flip_to_transient(self):
        """Mutating settings mid-run drops the held connection."""
        snapshots = []
        settings = et.MonitorSettings(
            persistent=True, poll_seconds=0.01,
            diagnostics_seconds=0.0, reconnect_base=0.01, reconnect_max=0.02)

        async def go():
            queue = asyncio.Queue()

            async def flip_later():
                # After the first snapshot, switch to transient and wake.
                while not snapshots:
                    await asyncio.sleep(0.005)
                settings.persistent = False
                queue.put_nowait((et.time.monotonic(), {}))

            flipper = asyncio.ensure_future(flip_later())
            await run_monitor_cycles(snapshots, 3, settings=settings,
                                     change_queue=queue)
            flipper.cancel()
        self.run_async(go())
        # The persistent session was closed and new sessions opened.
        self.assertGreaterEqual(len(FakeSession.instances), 2)
        self.assertTrue(FakeSession.instances[0].exited)


if __name__ == "__main__":
    unittest.main(verbosity=2)
