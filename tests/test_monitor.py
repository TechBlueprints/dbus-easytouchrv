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
        session_factory=kw.pop("session_factory", FakeSession),
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


class SlowExitSession(FakeSession):
    """FakeSession whose __aexit__ needs the loop to keep running: the
    disconnect only completes if cancellation semantics let cleanup await."""

    async def __aexit__(self, *exc):
        await asyncio.sleep(0.02)
        self.exited = True


class ShutdownDisconnectTests(unittest.TestCase):
    """The disconnect must complete even when the owning task is cancelled
    (the stale-BlueZ-link restart quirk: a lost disconnect leaves the LE
    link up and the thermostat never advertises again)."""

    def setUp(self):
        reset_fake()

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_monitor_cancel_completes_session_exit(self):
        """Cancelling the monitor mid-session still runs __aexit__ fully."""
        snapshots = []

        async def go():
            await run_monitor_cycles(snapshots, 1,
                                     session_factory=SlowExitSession)
        self.run_async(go())
        self.assertEqual(len(FakeSession.instances), 1)
        self.assertTrue(FakeSession.instances[0].exited)

    def test_aexit_disconnect_survives_cancellation(self):
        """A cancellation landing DURING the disconnect wait does not kill
        the in-flight disconnect (asyncio.shield in __aexit__), and the
        cancellation is still honored afterwards."""
        started = asyncio.Event()
        finished = asyncio.Event()

        class FakeClient:
            async def disconnect(self):
                started.set()
                await asyncio.sleep(0.05)
                finished.set()

        async def go():
            s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
            s._client = FakeClient()
            task = asyncio.ensure_future(s.__aexit__(None, None, None))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(finished.is_set())
            self.assertIsNone(s._client)
        self.run_async(go())

    def test_aexit_disconnect_timeout_gives_up(self):
        """A hung disconnect is abandoned after the session's timeout."""
        cancelled = asyncio.Event()

        class HungClient:
            async def disconnect(self):
                try:
                    await asyncio.sleep(60)
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

        async def go():
            s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
            s.DISCONNECT_TIMEOUT = 0.05
            s._client = HungClient()
            await s.__aexit__(None, None, None)   # returns, no hang
            await asyncio.sleep(0)                # let the cancel land
            self.assertTrue(cancelled.is_set())
        self.run_async(go())


class StaleLinkSweepTests(unittest.TestCase):
    """The startup self-remediation: after N failed find attempts the
    session sweeps BlueZ for a stale LE link before scanning again."""

    def setUp(self):
        reset_fake()
        self._orig_scanner = et.BleakScanner
        self._orig_establish = et.establish_connection
        self._orig_sweep = et.disconnect_stale_link

    def tearDown(self):
        et.BleakScanner = self._orig_scanner
        et.establish_connection = self._orig_establish
        et.disconnect_stale_link = self._orig_sweep

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _install(self, find_results, sweep_log):
        """Stub the scanner (scripted find results), the connector and the
        sweep hook.  sweep_log records how many finds had run at sweep time."""
        finds = []

        class StubScanner:
            @staticmethod
            async def find_device_by_address(address, timeout=0):
                result = find_results[len(finds)]
                finds.append(address)
                if result:
                    return types.SimpleNamespace(
                        name="EasyTouch 347000123", address=address)
                return None

        class StubClient:
            mtu_size = 23

            async def disconnect(self):
                pass

        async def fake_establish(*a, **k):
            return StubClient()

        async def fake_sweep(address):
            sweep_log.append(len(finds))
            return True

        et.BleakScanner = StubScanner
        et.establish_connection = fake_establish
        et.disconnect_stale_link = fake_sweep
        return finds

    def test_sweep_after_n_failed_finds_then_device_appears(self):
        sweeps = []
        finds = self._install([False, False, True], sweeps)

        async def go():
            s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
            await s.__aenter__()
            await s.__aexit__(None, None, None)
        self.run_async(go())
        # Sweep ran exactly once, after exactly STALE_SWEEP_AFTER (=2)
        # failed find attempts, and the third attempt then succeeded.
        self.assertEqual(sweeps, [et.EasyTouchSession.STALE_SWEEP_AFTER])
        self.assertEqual(len(finds), 3)

    def test_no_sweep_when_found_immediately(self):
        sweeps = []
        finds = self._install([True], sweeps)

        async def go():
            s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
            await s.__aenter__()
            await s.__aexit__(None, None, None)
        self.run_async(go())
        self.assertEqual(sweeps, [])
        self.assertEqual(len(finds), 1)

    def test_sweep_once_even_when_never_found(self):
        sweeps = []
        self._install([False, False, False], sweeps)

        async def go():
            s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
            with self.assertRaises(ConnectionError):
                await s.__aenter__()
        self.run_async(go())
        self.assertEqual(sweeps, [et.EasyTouchSession.STALE_SWEEP_AFTER])

    def test_disconnect_stale_link_degrades_without_bluez(self):
        """On non-Linux / stubbed stacks the sweep is a silent no-op: the
        bleak stub has no bluezdbus backend, so the helper returns False."""
        result = self.run_async(
            et.disconnect_stale_link("AA:BB:CC:DD:EE:FF"))
        self.assertIs(result, False)


class LinkGoneTests(unittest.TestCase):
    """A dead link must end the session at once, quietly.

    Each retained client holds its own D-Bus connection (bleak's BlueZ
    backend builds a MessageBus per client), and the system bus has a
    per-UID ceiling -- 152 of prod's 256 were held by this driver once a
    reconnect loop started.  Two extra 6s poll timeouts per cycle kept a
    dead client alive that much longer and wrote a full traceback each
    time, which rotated the log's own evidence away in minutes.
    """

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_classifies_dead_link_errors(self):
        for exc in (OSError(9, "Bad file descriptor"),
                    KeyError("152 is not registered"),
                    Exception("[org.bluez.Error.Failed] Not connected"),
                    Exception("Disconnected early")):
            self.assertTrue(et._is_link_gone(exc), exc)

    def test_keeps_ordinary_errors_retryable(self):
        for exc in (ValueError("bad payload"),
                    Exception("[org.bluez.Error.Failed] Unlikely error")):
            self.assertFalse(et._is_link_gone(exc), exc)

    def _session_with_client(self, client):
        s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
        s._client = client
        return s

    def test_write_failure_on_dead_link_raises(self):
        class DeadWrite:
            async def write_gatt_char(self, *a, **k):
                raise OSError(9, "Bad file descriptor")
        s = self._session_with_client(DeadWrite())
        with self.assertRaises(ConnectionError):
            self.run_async(s._exchange({"Type": "Get Status"}))

    def test_read_failure_on_dead_link_raises(self):
        class DeadRead:
            async def write_gatt_char(self, *a, **k):
                return None
            async def read_gatt_char(self, *a, **k):
                raise Exception("[org.bluez.Error.Failed] Not connected")
        s = self._session_with_client(DeadRead())
        with self.assertRaises(ConnectionError):
            self.run_async(s._exchange({"Type": "Get Status"}))

    def test_ordinary_write_failure_still_returns_none(self):
        """A non-link error keeps the old behaviour: no session teardown."""
        class OddWrite:
            async def write_gatt_char(self, *a, **k):
                raise ValueError("bad payload")
        s = self._session_with_client(OddWrite())
        self.assertIsNone(
            self.run_async(s._exchange({"Type": "Get Status"})))


class BackoffTests(unittest.TestCase):
    """Backoff must survive a unit that connects and instantly drops.

    Field case: 'AC Front' accepted every connection and failed the next
    GATT call ~33 ms later.  Resetting the backoff on connect meant it
    retried every 30 s indefinitely -- 66 cycles/hour, each leaking a
    D-Bus connection, until the system bus hit its per-UID ceiling.
    """

    def setUp(self):
        reset_fake()

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_backoff_grows_when_connect_succeeds_but_polls_fail(self):
        class InstantDropSession(FakeSession):
            async def get_status(self, zone):
                return None

        snapshots = []
        with self.assertLogs("easytouch_ble", level="INFO") as caught:
            self.run_async(run_monitor_cycles(
                snapshots, 7, session_factory=InstantDropSession,
                settings=et.MonitorSettings(
                    persistent=True, poll_seconds=0.001,
                    diagnostics_seconds=0.0,
                    reconnect_base=0.001, reconnect_max=0.05)))

        attempts = [int(m.rsplit("attempt ", 1)[1].rstrip(")"))
                    for m in caught.output if "Reconnecting in" in m]
        self.assertGreaterEqual(len(attempts), 2, caught.output)
        self.assertEqual(attempts[:2], [1, 2],
                         "backoff reset on connect: %r" % attempts)


class OrphanedBusTests(unittest.TestCase):
    """bleak leaks its per-session D-Bus connection on an abnormal drop.

    bleak's BlueZ backend opens a MessageBus per connection session and
    closes it at the END of disconnect().  If the device is already gone,
    assert_reply raises first and that close is never reached, and
    _cleanup_all() leaves _bus attached despite its docstring.  Measured on
    prod: +1 system-bus connection per retry, until 152 of 256 were held
    and the whole box could not open a new connection.
    """

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _client(self, disconnect_exc=None, bus_present=True):
        class Bus:
            def __init__(self):
                self.closed = False
            def disconnect(self):
                self.closed = True

        class Backend:
            pass

        class Client:
            def __init__(self):
                self._backend = Backend()
                self._backend._bus = Bus() if bus_present else None
            async def disconnect(self):
                if disconnect_exc is not None:
                    raise disconnect_exc
        return Client()

    def test_closes_bus_when_disconnect_raises(self):
        """The device-already-gone path: bleak skips its own close."""
        client = self._client(
            disconnect_exc=Exception("[org.bluez.Error.Failed] Not connected"))
        s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
        s._client = client
        bus = client._backend._bus
        self.run_async(s.__aexit__())
        self.assertTrue(bus.closed, "orphaned bus was not closed")
        self.assertIsNone(client._backend._bus)

    def test_no_op_when_bleak_already_closed_it(self):
        """A clean disconnect nulls _bus; we must not double-close."""
        client = self._client(bus_present=False)
        s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
        s._client = client
        self.run_async(s.__aexit__())
        self.assertIsNone(client._backend._bus)

    def test_degrades_when_internals_absent(self):
        """A bleak without _backend/_bus must not break the session exit."""
        class Bare:
            async def disconnect(self):
                return None
        s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
        s._client = Bare()
        self.run_async(s.__aexit__())   # must not raise


class PhantomConnectTests(unittest.TestCase):
    """A phantom connect must clear BlueZ state before the next attempt.

    Field case, 21 hours: a BlueZ Device1 object for AC Front sat stuck
    Connected=true on hci1 with NO ACL link on any adapter.  bleak's
    connect() believes that property, returns success, and falls back to
    the default MTU of 23 -- so "Connected (MTU: 23)" appeared in the log
    exactly like a real connection while every GATT call failed "Not
    connected" ~33 ms later.  The old sweep never ran because it only
    triggered when the device could not be FOUND, and a phantom is
    perfectly findable.
    """

    def setUp(self):
        et._phantom_connects.clear()

    def tearDown(self):
        et._phantom_connects.clear()

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_link_gone_marks_the_address(self):
        class DeadWrite:
            async def write_gatt_char(self, *a, **k):
                raise Exception("[org.bluez.Error.Failed] Not connected")
        s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
        s._client = DeadWrite()
        with self.assertRaises(ConnectionError):
            self.run_async(s._exchange({"Type": "Get Config"}))
        self.assertTrue(et._phantom_connects.get("AA:BB:CC:DD:EE:FF"))

    def test_sweep_runs_before_scanning_after_a_phantom(self):
        et._phantom_connects["AA:BB:CC:DD:EE:FF"] = True
        swept = []

        async def fake_sweep(address, *a, **k):
            swept.append(address)
            return True

        real = et.disconnect_stale_link
        et.disconnect_stale_link = fake_sweep
        try:
            s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
            try:
                self.run_async(s.__aenter__())
            except Exception:
                pass          # no radio in tests; the sweep is the assertion
        finally:
            et.disconnect_stale_link = real
        self.assertEqual(swept, ["AA:BB:CC:DD:EE:FF"])

    def test_no_sweep_when_no_phantom_recorded(self):
        swept = []

        async def fake_sweep(address, *a, **k):
            swept.append(address)
            return True

        real = et.disconnect_stale_link
        et.disconnect_stale_link = fake_sweep
        try:
            s = et.EasyTouchSession("AA:BB:CC:DD:EE:FF")
            try:
                self.run_async(s.__aenter__())
            except Exception:
                pass
        finally:
            et.disconnect_stale_link = real
        self.assertEqual(swept, [])

    def test_live_data_clears_phantom_state(self):
        et._phantom_connects["AA:BB:CC:DD:EE:FF"] = True
        et.note_link_alive("aa:bb:cc:dd:ee:ff")
        self.assertNotIn("AA:BB:CC:DD:EE:FF", et._phantom_connects)


class SilentUnitTests(unittest.TestCase):
    """A unit that never advertises must not be logged as a lost connection.

    The old wording emitted "Monitor connection lost: Device not found
    after scanning" -- nothing was lost and nothing was found.  An
    experienced reader took the prefix at face value and diagnosed
    "connects but cannot hold the link" when the truth was the opposite,
    which sent the investigation at the BLE stack instead of at the unit.
    The two cases have disjoint remedies: a dropped link is ours to retry,
    a silent unit is held by another central, unpowered, or out of range.
    """

    def setUp(self):
        reset_fake()

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_not_found_is_its_own_exception_type(self):
        self.assertTrue(issubclass(et.DeviceNotFound, ConnectionError))

    def test_silent_unit_logged_as_silent_not_lost(self):
        class SilentSession(FakeSession):
            async def __aenter__(self):
                raise et.DeviceNotFound("[AA:BB:CC:DD:EE:FF] not seen in 3 scan windows")

        snapshots = []
        with self.assertLogs("easytouch_ble", level="INFO") as caught:
            self.run_async(run_monitor_cycles(
                snapshots, 2, session_factory=SilentSession,
                settings=et.MonitorSettings(
                    persistent=True, poll_seconds=0.001,
                    diagnostics_seconds=0.0,
                    reconnect_base=0.001, reconnect_max=0.01)))
        text = "\n".join(caught.output)
        self.assertIn("silent", text.lower())
        self.assertNotIn("Monitor connection lost", text)

    def test_real_drop_still_logged_as_connection_lost(self):
        class DroppingSession(FakeSession):
            async def get_status(self, zone):
                return None

        snapshots = []
        with self.assertLogs("easytouch_ble", level="INFO") as caught:
            self.run_async(run_monitor_cycles(
                snapshots, 4, session_factory=DroppingSession,
                settings=et.MonitorSettings(
                    persistent=True, poll_seconds=0.001,
                    diagnostics_seconds=0.0,
                    reconnect_base=0.001, reconnect_max=0.01)))
        self.assertIn("Monitor connection lost", "\n".join(caught.output))


if __name__ == "__main__":
    unittest.main(verbosity=2)
