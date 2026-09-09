"""The driver must run on plain bleak + bleak-retry-connector.

bleak-connection-manager is optional: /data/bcm may be absent (a checkout
without it, or a box that never ran install.sh step 2b).  These tests load
easytouch_ble and catcher_setup with the BCM imports made to fail, so the
fallback path is exercised rather than assumed.
"""
import asyncio
import builtins
import importlib
import importlib.util
import logging
import os
import sys
import types
import unittest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO_ROOT)

for name in ("bleak",):
    mod = types.ModuleType(name)
    mod.BleakClient = object
    mod.BleakScanner = object
    sys.modules.setdefault(name, mod)
brc = types.ModuleType("bleak_retry_connector")
brc.establish_connection = lambda *a, **k: None
sys.modules.setdefault("bleak_retry_connector", brc)


class _NoBCM:
    """Context manager: BCM imports raise ImportError inside the block."""

    def __enter__(self):
        self._saved = {k: v for k, v in sys.modules.items()
                       if k.startswith("bleak_connection_manager")}
        for k in self._saved:
            del sys.modules[k]
        self._real_import = builtins.__import__

        def _fake(name, *a, **kw):
            if name.startswith("bleak_connection_manager"):
                raise ImportError("no bleak_connection_manager (test)")
            return self._real_import(name, *a, **kw)

        builtins.__import__ = _fake
        return self

    def __exit__(self, *exc):
        builtins.__import__ = self._real_import
        sys.modules.update(self._saved)
        return False


def _load_without_bcm(module_name, alias):
    """Load a repo module under `alias` with BCM unimportable.

    Loading under a different name matters: replacing the canonical
    sys.modules entry would hand later tests a second copy of the module,
    so their `except DeviceNotFound` would no longer catch this copy's
    class and 28 unrelated tests fail depending on file order.
    """
    path = os.path.join(_REPO_ROOT, module_name + ".py")
    with _NoBCM():
        spec = importlib.util.spec_from_file_location(alias, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[alias] = mod          # dataclasses resolve via sys.modules
        try:
            spec.loader.exec_module(mod)
        finally:
            sys.modules.pop(alias, None)
    return mod


class FakeChar:
    def __init__(self, uuid):
        self.uuid = uuid


class FakeService:
    def __init__(self, *uuids):
        self.characteristics = [FakeChar(u) for u in uuids]


class FakeClient:
    def __init__(self, services):
        self.services = services
        self.address = "AA:BB:CC:DD:EE:FF"


class FallbackValidatorTests(unittest.TestCase):
    def setUp(self):
        self.et = _load_without_bcm("easytouch_ble", "_et_no_bcm")

    def test_module_imports_without_bcm(self):
        self.assertFalse(self.et.HAVE_BCM_VALIDATORS)

    def _check(self, client):
        v = self.et.tolerate_late_gatt(
            self.et.validate_char_exists(self.et.RETURN_UUID))
        # A private loop, not asyncio.run(): run() nulls the thread's
        # current event loop on exit, and the other test modules call
        # get_event_loop(), which then raises on 3.9.  new_event_loop()
        # never touches the ambient setting.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(v(client))
        finally:
            loop.close()

    def test_accepts_link_exposing_the_vendor_characteristic(self):
        client = FakeClient([FakeService("0000180a-0000-1000-8000-00805f9b34fb"),
                             FakeService(self.et.RETURN_UUID.upper())])
        self.assertTrue(self._check(client))

    def test_rejects_link_without_the_characteristic(self):
        client = FakeClient([FakeService("0000180a-0000-1000-8000-00805f9b34fb")])
        self.assertFalse(self._check(client))

    def test_rejects_phantom_link_with_empty_gatt(self):
        # The connect that "succeeds" with no services resolved at all.
        self.assertFalse(self._check(FakeClient([])))
        self.assertFalse(self._check(FakeClient(None)))


class ContractStartupLineTests(unittest.TestCase):
    """The CONSUMERS.md startup lines are a fleet interface, not prose.

    A monitor greps these across services, so the anchor and wording are
    pinned verbatim here; paraphrasing one silently drops this service out
    of fleet monitoring.
    """

    ANCHOR = "BLE coordination: "

    def _catcher_setup(self):
        cs = _load_without_bcm("catcher_setup", "_cs_no_bcm")
        cs.catcher_options = lambda: (["AA:BB:CC:DD:EE:FF@hci1", "hci2"],
                                      {"hci1": 5})
        return cs

    def test_no_shared_install_warns_and_names_ignored_routing(self):
        cs = self._catcher_setup()
        cs.shared_dir = lambda: "/nonexistent/bcm"
        with _NoBCM():
            with self.assertLogs(cs.logger, level=logging.WARNING) as cm:
                self.assertFalse(cs.install_catcher_from_config("test"))
        blob = "\n".join(cm.output)
        self.assertIn(
            "BLE coordination: no shared install at /nonexistent/bcm; "
            "running uncoordinated, no claims, no adapter routing, "
            "no card recovery", blob)
        # Silence about ignored routing is the dangerous outcome: a pin
        # reads as honoured while BlueZ picks the card.
        self.assertIn("AA:BB:CC:DD:EE:FF@hci1", blob)
        self.assertIn("hci1:5", blob)

    def test_empty_dir_collapses_into_the_no_install_line(self):
        # One key, so "never look" is reported as absence rather than as
        # the separate DIR-empty line the contract reserves for two keys.
        cs = self._catcher_setup()
        cs.shared_dir = lambda: ""
        with _NoBCM():
            with self.assertLogs(cs.logger, level=logging.WARNING) as cm:
                self.assertFalse(cs.install_catcher_from_config("test"))
        self.assertIn("BLE coordination: no shared install at ",
                      "\n".join(cm.output))

    def test_every_coordination_line_carries_the_anchor(self):
        cs = self._catcher_setup()
        cs.shared_dir = lambda: "/nonexistent/bcm"
        with _NoBCM():
            with self.assertLogs(cs.logger, level=logging.WARNING) as cm:
                cs.install_catcher_from_config("test")
        coordination = [r for r in cm.output if "coordination" in r]
        self.assertTrue(coordination)
        for record in coordination:
            self.assertIn(self.ANCHOR, record)


class SharedInstallLineTests(unittest.TestCase):
    """The lines emitted when a shared install IS found and used."""

    def _fake_shared(self, tmp, install=None, signature_has_fsn=True):
        """Build a fake /data/bcm tree and a stub package inside it."""
        pkg = os.path.join(tmp, "src", "bleak_connection_manager")
        os.makedirs(pkg, exist_ok=True)
        open(os.path.join(pkg, "__init__.py"), "w").close()

        mod = types.ModuleType("bleak_connection_manager")
        mod.__file__ = os.path.join(pkg, "__init__.py")
        if signature_has_fsn:
            def install_bleak_catcher(owner, adapters=(), link_caps=None,
                                      wrap_scanner=False,
                                      tune_conn_params=True,
                                      force_start_notify=None):
                if install:
                    install(owner, adapters=adapters)
        else:
            def install_bleak_catcher(owner, adapters=(), link_caps=None,
                                      wrap_scanner=False,
                                      tune_conn_params=True):
                if install:
                    install(owner, adapters=adapters)
        mod.install_bleak_catcher = install_bleak_catcher
        return mod

    def _run(self, tmp, mod, adapters):
        import catcher_setup as cs
        importlib.reload(cs)
        cs.catcher_options = lambda: (adapters, {})
        cs.shared_dir = lambda: tmp
        saved = sys.modules.get("bleak_connection_manager")
        sys.modules["bleak_connection_manager"] = mod   # -> "provided"
        try:
            with self.assertLogs(cs.logger, level=logging.INFO) as cm:
                ok = cs.install_catcher_from_config("test")
            return ok, "\n".join(cm.output)
        finally:
            if saved is None:
                sys.modules.pop("bleak_connection_manager", None)
            else:
                sys.modules["bleak_connection_manager"] = saved

    def test_loaded_from_names_the_package_dir_not_the_config_key(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._fake_shared(tmp)
            ok, blob = self._run(tmp, mod, ["hci1", "AA:BB@hci1"])
            self.assertTrue(ok)
            # The package's own directory proves which tree served; the
            # configured key would not.
            self.assertIn(
                "BLE coordination: bleak_connection_manager loaded from "
                + os.path.join(tmp, "src", "bleak_connection_manager"), blob)

    def test_catcher_installed_line_reports_policy_and_counts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._fake_shared(tmp)
            ok, blob = self._run(tmp, mod, ["hci1", "AA:BB@hci1"])
            self.assertTrue(ok)
            self.assertIn(
                "BLE coordination: catcher installed (force_start_notify=True, "
                "adapters=2 configured, 1 pinned)", blob)

    def test_old_install_falls_back_to_the_legacy_env(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._fake_shared(tmp, signature_has_fsn=False)
            os.environ.pop("BCM_FORCE_START_NOTIFY", None)
            ok, blob = self._run(tmp, mod, ["hci1"])
            self.assertTrue(ok)
            self.assertIn("predates the force_start_notify parameter", blob)
            self.assertEqual(os.environ.get("BCM_FORCE_START_NOTIFY"), "true")
            os.environ.pop("BCM_FORCE_START_NOTIFY", None)

    def test_catcher_raising_is_distinct_from_a_broken_install(self):
        import tempfile
        def boom(owner, adapters=()):
            raise ValueError("bad kwarg")
        with tempfile.TemporaryDirectory() as tmp:
            mod = self._fake_shared(tmp, install=boom)
            ok, blob = self._run(tmp, mod, ["hci1"])
            self.assertFalse(ok)
            # Operator action is "fix the driver", not "fix the install".
            self.assertIn("BLE coordination: catcher would not install from",
                          blob)
            self.assertNotIn("present but unusable", blob)


class RunScriptTests(unittest.TestCase):
    """Pins the launcher template.

    CONSUMERS.md rule 7 is explicit that the box-side check on the file
    /service/<name> RESOLVES to is the real gate and this test never
    replaces it. It is still worth having: it stops the shim exec coming
    back into the repo, and it guards the subtler trap that BCM's
    installer greps these run scripts for a shim reference in ANY form --
    so even naming the path in a comment would keep the shim alive on the
    box after the last real user was gone.
    """

    def _run_script(self):
        path = os.path.join(_REPO_ROOT, "service", "run")
        with open(path) as fh:
            return fh.read()

    def test_launches_plain_python3(self):
        self.assertIn("exec softlimit", self._run_script())
        self.assertIn("python3 -u dbus-easytouchrv.py", self._run_script())

    def test_no_shim_reference_in_any_form(self):
        import re
        # The installer's own pattern.
        pattern = re.compile(r"bcm[^\s\"']*/python3|BCM_PY")
        for n, line in enumerate(self._run_script().splitlines(), 1):
            self.assertIsNone(
                pattern.search(line),
                "service/run:%d still references the interpreter shim "
                "(comments count -- the installer greps this file): %r"
                % (n, line))


if __name__ == "__main__":
    unittest.main()
