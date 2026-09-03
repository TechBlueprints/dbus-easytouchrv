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


class CatcherFallbackTests(unittest.TestCase):
    def test_install_returns_false_and_warns_about_ignored_routing(self):
        # The import must fail at CALL time, not just at module load: a
        # sibling test module leaves a stubbed bleak_connection_manager in
        # sys.modules, which would otherwise satisfy the import and make
        # this pass alone but not in the suite.
        cs = _load_without_bcm("catcher_setup", "_cs_no_bcm")
        with _NoBCM():
            cs.catcher_options = lambda: (["AA:BB:CC:DD:EE:FF@hci1"],
                                          {"hci1": 5})
            with self.assertLogs(cs.logger, level=logging.WARNING) as cm:
                self.assertFalse(cs.install_catcher_from_config("test"))
        blob = "\n".join(cm.output)
        # The operator must be told their routing is being ignored, not
        # left to infer it from a link landing on the wrong radio.
        self.assertIn("bleak-connection-manager not available", blob)
        self.assertIn("AA:BB:CC:DD:EE:FF@hci1", blob)
        self.assertIn("hci1:5", blob)


if __name__ == "__main__":
    unittest.main()
