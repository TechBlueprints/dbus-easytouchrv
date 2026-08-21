"""Smoke test for ThermostatDevice with stubbed dbus/GLib/vedbus.

Exercises the real driver code: output creation from config, status
application, fan dropdown rebuild on mode change, GUI write callbacks and
change coalescing.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
import types
import unittest

# ── Stub BLE deps (as in test_logic) ────────────────────────────────────────
for name in ("bleak",):
    mod = types.ModuleType(name)
    mod.BleakClient = object
    mod.BleakScanner = object
    sys.modules.setdefault(name, mod)

brc = types.ModuleType("bleak_retry_connector")
brc.establish_connection = lambda *a, **k: None
sys.modules.setdefault("bleak_retry_connector", brc)

# The driver installs the bleak catcher at import; a no-op stub keeps the
# smoke test radio-free.
bcm = types.ModuleType("bleak_connection_manager")
bcm.install_bleak_catcher = lambda *a, **k: None
sys.modules.setdefault("bleak_connection_manager", bcm)

# ── Stub dbus / gi / vedbus ─────────────────────────────────────────────────
dbus_mod = types.ModuleType("dbus")
dbus_mod.SystemBus = lambda private=False: "systembus"
dbus_mod.SessionBus = lambda private=False: "sessionbus"
dbus_mod.exceptions = types.SimpleNamespace(DBusException=Exception)
sys.modules["dbus"] = dbus_mod
mainloop_pkg = types.ModuleType("dbus.mainloop")
mainloop_glib = types.ModuleType("dbus.mainloop.glib")
mainloop_glib.DBusGMainLoop = lambda **k: None
sys.modules["dbus.mainloop"] = mainloop_pkg
sys.modules["dbus.mainloop.glib"] = mainloop_glib

_timers = {}
_timer_seq = [0]

class _GLibStub:
    @staticmethod
    def timeout_add(ms, cb, *a):
        _timer_seq[0] += 1
        _timers[_timer_seq[0]] = (cb, a)
        return _timer_seq[0]
    timeout_add_seconds = timeout_add

    @staticmethod
    def source_remove(tid):
        _timers.pop(tid, None)

    @staticmethod
    def idle_add(cb, *a):
        cb(*a)
        return 0

gi = types.ModuleType("gi")
gi.repository = types.SimpleNamespace(GLib=_GLibStub)
sys.modules["gi"] = gi
sys.modules["gi.repository"] = gi.repository if isinstance(gi.repository, types.ModuleType) else types.ModuleType("gi.repository")
sys.modules["gi.repository"].GLib = _GLibStub


class FakeVeDbusService:
    """Dict-backed stand-in for VeDbusService."""
    def __init__(self, name, bus=None, register=None):
        self.name = name
        self.values = {}
        self.callbacks = {}
        self.registered = False

    def add_path(self, path, value, description="", writeable=False,
                 onchangecallback=None, **kw):
        self.values[path] = value
        if onchangecallback:
            self.callbacks[path] = onchangecallback

    def register(self):
        self.registered = True

    def __contains__(self, path):
        return path in self.values

    def __getitem__(self, path):
        return self.values[path]

    def __setitem__(self, path, value):
        assert path in self.values, "set on unknown path %s" % path
        self.values[path] = value

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass

    # simulate an external SetValue (GUI write)
    def external_write(self, path, value):
        cb = self.callbacks[path]
        ok = cb(path, value)
        if ok:
            self.values[path] = value
        return ok


vedbus_mod = types.ModuleType("vedbus")
vedbus_mod.VeDbusService = FakeVeDbusService
sys.modules["vedbus"] = vedbus_mod
sys.modules["settingsdevice"] = types.ModuleType("settingsdevice")
sys.modules["settingsdevice"].SettingsDevice = object

sys.path.insert(0, _REPO_ROOT)

import importlib.util
spec = importlib.util.spec_from_file_location(
    "driver", os.path.join(_REPO_ROOT, "dbus-easytouchrv.py"))
driver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(driver)

import easytouch_ble as et


class FakeManager:
    bus = "systembus"
    freeze_input_enabled = True
    def __init__(self):
        self.dispatched = []
        self.names = {}
    def enqueue_change(self, dev, changes):
        self.dispatched.append((dev.dev_id, changes))
    def custom_name(self, dev_id, kind, default):
        return self.names.get((dev_id, kind), default)
    def set_custom_name(self, dev_id, kind, value):
        self.names[(dev_id, kind)] = value


def make_status(**kw):
    base = dict(zone=0, mode=et.MODE_COOL, inside_temp=73, outside_temp=88,
                cool_sp=74, heat_sp=68, dry_sp=72, auto_cool_sp=76,
                auto_heat_sp=70, fault=0, activity=2, system_power=True,
                fan_fanonly=1, fan_cool=0x80, fan_elec=2, fan_auto=0x80,
                fan_gas=1, rev="1.0.6.5")
    base.update(kw)
    return et.ZoneStatus(**base)


def make_config():
    cfg = et.ZoneConfig(zone=0)
    # Off, FanOnly, Cool, Heat(3), Auto(8)
    cfg.available_modes = (1 << 0) | (1 << 1) | (1 << 2) | (1 << 3) | (1 << 8)
    cfg.min_cool, cfg.max_cool, cfg.min_heat, cfg.max_heat = 65, 85, 45, 80
    fa = [0] * 16
    fa[1] = 0x80 | 0x20 | 3   # fan only: auto + 3 speeds + off
    fa[2] = 0x80 | 3          # cool: auto + 3 speeds (no off)
    fa[3] = 0x10 | 1          # heat: fixed speed
    fa[8] = 0x80 | 3
    cfg.fan_array = fa
    return cfg


class ThermostatDeviceTests(unittest.TestCase):
    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.dev = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "Bedroom A/C", 0, "1234", 150)

    def fire_timers(self):
        for tid, (cb, a) in list(_timers.items()):
            _timers.pop(tid, None)
            cb(*a)

    def test_services_registered(self):
        self.assertTrue(self.dev._temp.registered)
        self.assertTrue(self.dev._switch.registered)
        self.assertEqual(self.dev._switch.name,
                         "com.victronenergy.switch.easytouch_aabbccddeeff")

    def test_outputs_created_from_config(self):
        self.dev.apply_config(make_config())
        sw = self.dev._switch.values
        # power folded into the mode dropdown; no separate toggle
        self.assertNotIn("/SwitchableOutput/power/State", sw)
        self.assertNotIn("/SwitchableOutput/pollint/Dimming", sw)
        self.assertIn("/SwitchableOutput/mode/Dimming", sw)
        self.assertEqual(sw["/SwitchableOutput/mode/Settings/Labels"],
                         ["Off", "Fan Only", "Cool", "Gas Heat", "Auto"])
        self.assertIn("/SwitchableOutput/cool/Dimming", sw)
        self.assertIn("/SwitchableOutput/heat/Dimming", sw)
        self.assertIn("/SwitchableOutput/fan/Dimming", sw)
        # slider ranges: the unit's own limits, exact so steps land on whole F
        self.assertAlmostEqual(
            sw["/SwitchableOutput/cool/Settings/DimmingMin"] * 9 / 5 + 32, 65.0)
        self.assertAlmostEqual(
            sw["/SwitchableOutput/cool/Settings/DimmingMax"] * 9 / 5 + 32, 85.0)
        self.assertAlmostEqual(
            sw["/SwitchableOutput/heat/Settings/DimmingMin"] * 9 / 5 + 32, 45.0)
        # one step == exactly one degree F, displayed without decimals
        for out in ("cool", "heat"):
            step = sw["/SwitchableOutput/%s/Settings/StepSize" % out]
            self.assertAlmostEqual(step * 9 / 5, 1.0)
            self.assertEqual(sw["/SwitchableOutput/%s/Settings/Decimals" % out], 0)
        self.assertEqual(sw["/SwitchableOutput/mode/Settings/Type"], 6)
        self.assertEqual(sw["/SwitchableOutput/cool/Settings/Type"], 3)
        self.assertEqual(sw["/SwitchableOutput/mode/Settings/Group"], "Bedroom A/C")

    def test_status_application(self):
        self.dev.apply_config(make_config())
        self.dev.apply_status(make_status())
        sw = self.dev._switch.values
        t = self.dev._temp.values
        self.assertAlmostEqual(t["/Temperature"], 22.8)
        self.assertEqual(t["/Status"], 0)
        self.assertEqual(t["/FirmwareVersion"], "1.0.6.5")
        # mode Cool -> index 2 in [Off, FanOnly, Cool, GasHeat, Auto]
        self.assertEqual(sw["/SwitchableOutput/mode/Dimming"], 2)
        self.assertAlmostEqual(
            sw["/SwitchableOutput/cool/Dimming"] * 9 / 5 + 32, 74.0)
        self.assertAlmostEqual(
            sw["/SwitchableOutput/heat/Dimming"] * 9 / 5 + 32, 68.0)
        self.assertAlmostEqual(sw["/SwitchableOutput/cool/Measurement"], 22.8)
        # fan: cool FA -> [Auto, Low, Medium, High]; value 0x80 -> idx 0
        self.assertEqual(sw["/SwitchableOutput/fan/Settings/Labels"],
                         ["Auto", "Low", "Medium", "High"])
        self.assertEqual(sw["/SwitchableOutput/fan/Dimming"], 0)
        self.assertEqual(sw["/SwitchableOutput/mode/Status"], 0x09)

    def test_fan_rebuild_on_mode_change(self):
        self.dev.apply_config(make_config())
        self.dev.apply_status(make_status())
        # switch to fan-only: options now include Off
        self.dev.apply_status(make_status(mode=et.MODE_FAN_ONLY, fan_fanonly=2))
        sw = self.dev._switch.values
        self.assertEqual(sw["/SwitchableOutput/fan/Settings/Labels"],
                         ["Auto", "Low", "Medium", "High", "Off"])
        self.assertEqual(sw["/SwitchableOutput/fan/Dimming"], 2)  # speed 2 -> Medium
        # heat mode: fixed fan -> control hidden
        self.dev.apply_status(make_status(mode=et.MODE_GAS_HEAT))
        self.assertEqual(sw["/SwitchableOutput/fan/Settings/Labels"], [])
        self.assertEqual(sw["/SwitchableOutput/fan/Settings/ShowUIControl"], 0)

    def test_auto_mode_setpoints(self):
        self.dev.apply_config(make_config())
        self.dev.apply_status(make_status(mode=et.MODE_AUTO))
        sw = self.dev._switch.values
        self.assertAlmostEqual(
            sw["/SwitchableOutput/cool/Dimming"] * 9 / 5 + 32, 76.0)
        self.assertAlmostEqual(
            sw["/SwitchableOutput/heat/Dimming"] * 9 / 5 + 32, 70.0)

    def test_write_pipeline(self):
        self.dev.apply_config(make_config())
        self.dev.apply_status(make_status())
        sw = self.dev._switch

        # GUI sets mode to Auto (index 4) and cool to 25.0 C (77 F)
        self.assertTrue(sw.external_write("/SwitchableOutput/mode/Dimming", 4))
        self.assertTrue(sw.external_write("/SwitchableOutput/cool/Dimming", 25.0))
        # nothing dispatched until the coalescing timer fires
        self.assertEqual(self.mgr.dispatched, [])
        self.assertTrue(self.dev.edits_active)

        self.fire_timers()
        self.assertEqual(len(self.mgr.dispatched), 1)
        dev_id, changes = self.mgr.dispatched[0]
        # selecting a mode powers on and sets the mode in one batch; the
        # cool slider written afterwards targets the auto setpoint
        self.assertEqual(changes,
                         {"power": 1, "mode": et.MODE_AUTO, "autoCool_sp": 77})

        # while in flight, a poll status must not clobber control values
        self.assertTrue(self.dev.edits_active)
        self.dev.apply_status(make_status())  # stale: mode Cool
        self.assertEqual(sw.values["/SwitchableOutput/mode/Dimming"], 4)

        # change completes -> read-back applies
        self.dev.change_finished()
        self.assertFalse(self.dev.edits_active)
        # powered down -> dropdown shows Off, and mode stays interactive
        self.dev.apply_status(make_status(mode=et.MODE_AUTO, system_power=False))
        self.assertEqual(sw.values["/SwitchableOutput/mode/Dimming"], 0)
        self.assertEqual(sw.values["/SwitchableOutput/mode/Status"], 0x09)
        self.assertEqual(sw.values["/SwitchableOutput/fan/Status"], 0x20)

    def test_fan_write_uses_mode_key(self):
        self.dev.apply_config(make_config())
        self.dev.apply_status(make_status())  # mode Cool
        sw = self.dev._switch
        # select "High" (index 3 in [Auto, Low, Medium, High])
        self.assertTrue(sw.external_write("/SwitchableOutput/fan/Dimming", 3))
        self.fire_timers()
        _dev_id, changes = self.mgr.dispatched[0]
        self.assertEqual(changes, {"coolFan": 3})

    def test_dry_mode_cool_slider_maps_to_dry_sp(self):
        cfg = make_config()
        cfg.available_modes |= (1 << et.MODE_DRY)
        self.dev.apply_config(cfg)
        self.dev.apply_status(make_status(mode=et.MODE_DRY))
        sw = self.dev._switch
        self.assertAlmostEqual(
            sw.values["/SwitchableOutput/cool/Dimming"] * 9 / 5 + 32, 72.0)
        self.assertTrue(sw.external_write("/SwitchableOutput/cool/Dimming", 21.0))
        self.fire_timers()
        _dev_id, changes = self.mgr.dispatched[0]
        self.assertEqual(changes, {"dry_sp": 70})

    def test_disconnected(self):
        self.dev.apply_config(make_config())
        self.dev.apply_status(make_status())
        self.dev.set_disconnected()
        self.assertEqual(self.dev._temp.values["/Status"], 4)
        self.assertEqual(
            self.dev._switch.values["/SwitchableOutput/mode/Status"], 0x20)

    def test_no_modes_no_outputs(self):
        self.dev.apply_config(et.ZoneConfig(zone=0))
        self.assertNotIn("/SwitchableOutput/power/State", self.dev._switch.values)
        # status still updates the temperature service
        self.dev.apply_status(make_status())
        self.assertAlmostEqual(self.dev._temp.values["/Temperature"], 22.8)




class ExtraDataServiceTests(unittest.TestCase):
    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.dev = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "Main A/C", 0, "", 150, serial="347000111")

    def test_model_prefix_from_config(self):
        self.assertEqual(self.dev.model_prefix, "347")

    def test_learn_ble_name(self):
        dev = driver.ThermostatDevice(
            self.mgr, "11:22:33:44:55:66", "Aux A/C", 0, "", 155)
        self.assertEqual(dev.model_prefix, "")
        dev.learn_ble_name("EasyTouch 358987654")
        self.assertEqual(dev.model_prefix, "358")
        dev.learn_ble_name("EasyTouch 352000000")  # must not overwrite
        self.assertEqual(dev.model_prefix, "358")

    def test_apply_extra_creates_coil_sensors(self):
        ed = et.parse_extra_data(
            {"Zone": 0, "SS": 2, "OAT": 85, "InCoil": 55, "OutCoil": 95,
             "VDC": 1342}, "347")
        self.dev.apply_extra(ed)
        t = self.dev._temp.values
        self.assertAlmostEqual(t["/Voltage"], 13.42)
        # also surfaced where the GUI actually renders it
        self.assertAlmostEqual(
            self.dev._switch.values["/ModuleVoltage"], 13.42)
        self.assertAlmostEqual(t["/OutsideTemperature"], 29.4)
        self.assertEqual(t["/DiagnosticFlags"], "remote_sensor")
        incoil = self.dev._aux["incoil"]
        outcoil = self.dev._aux["outcoil"]
        self.assertEqual(incoil.name,
                         "com.victronenergy.temperature.easytouch_aabbccddeeff_incoil")
        self.assertEqual(incoil.values["/DeviceInstance"], 151)
        self.assertEqual(outcoil.values["/DeviceInstance"], 152)
        self.assertAlmostEqual(incoil.values["/Temperature"], 12.8)
        self.assertEqual(incoil.values["/Status"], 0)
        self.assertEqual(incoil.values["/CustomName"], "Main A/C in coil")

    def test_bad_coil_after_creation(self):
        ed = et.parse_extra_data({"InCoil": 55, "OutCoil": 95}, "347")
        self.dev.apply_extra(ed)
        ed2 = et.parse_extra_data({"InCoil": 255, "OutCoil": 95}, "347")
        self.dev.apply_extra(ed2)
        incoil = self.dev._aux["incoil"]
        self.assertIsNone(incoil.values["/Temperature"])
        self.assertEqual(incoil.values["/Status"], 4)

    def test_no_coils_no_aux_services(self):
        ed = et.parse_extra_data({"OS": 0x02, "SS": 0x30}, "352")
        self.dev.apply_extra(ed)
        self.assertEqual(self.dev._aux, {})
        self.assertIn("cooling", self.dev._temp.values["/DiagnosticFlags"])

    def test_disconnected_includes_aux(self):
        ed = et.parse_extra_data({"InCoil": 55}, "347")
        self.dev.apply_extra(ed)
        self.dev.set_disconnected()
        self.assertEqual(self.dev._aux["incoil"].values["/Status"], 4)

    def test_status_prm_extras(self):
        self.dev.apply_config(make_config())
        self.dev.apply_status(make_status(faceplate_temp=72, weather_temp=78))
        t = self.dev._temp.values
        self.assertAlmostEqual(t["/FaceplateTemperature"], 22.2)
        self.assertAlmostEqual(t["/WeatherTemperature"], 25.6)




class SetupOutputTests(unittest.TestCase):
    """The GUI-adjustable monitor settings exposed on the switch service."""

    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.mgr.setting_changes = []
        self.mgr.monitor_setting_changed = (
            lambda dev, key, value: self.mgr.setting_changes.append((key, value)))
        self.settings = et.MonitorSettings(persistent=True, poll_seconds=10.0)
        self.dev = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "Bedroom A/C", 0, "", 150,
            monitor_settings=self.settings)

    def test_setup_outputs_exist(self):
        sw = self.dev._switch.values
        self.assertEqual(sw["/SwitchableOutput/connection/Settings/Type"], 6)
        self.assertEqual(sw["/SwitchableOutput/connection/Settings/Labels"],
                         ["Persistent", "Intermittent"])
        self.assertEqual(sw["/SwitchableOutput/connection/Dimming"], 0)
        # setup controls share the thermostat's card, not a separate one
        self.assertEqual(sw["/SwitchableOutput/connection/Settings/Group"],
                         "Bedroom A/C")
        # poll interval is config-only; no GUI control for it
        self.assertNotIn("/SwitchableOutput/pollint/Dimming", sw)

    def test_connection_write(self):
        sw = self.dev._switch
        self.assertTrue(sw.external_write(
            "/SwitchableOutput/connection/Dimming", 1))
        self.assertFalse(self.settings.persistent)
        self.assertEqual(self.mgr.setting_changes, [("Persistent", 0)])
        self.assertTrue(sw.external_write(
            "/SwitchableOutput/connection/Dimming", 0))
        self.assertTrue(self.settings.persistent)

    def test_transient_initial_dimming(self):
        settings = et.MonitorSettings(persistent=False, poll_seconds=300.0)
        dev = driver.ThermostatDevice(
            self.mgr, "11:22:33:44:55:66", "Aux A/C", 0, "", 155,
            monitor_settings=settings)
        sw = dev._switch.values
        self.assertEqual(sw["/SwitchableOutput/connection/Dimming"], 1)




class OutsideAirSensorTests(unittest.TestCase):
    """Outside air is a first-class Outdoor-type sensor, with no flapping
    between the status and ExtraData sources."""

    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.dev = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "Main A/C", 0, "", 150,
            serial="347000111")
        self.dev.apply_config(make_config())

    def test_outside_sensor_from_status(self):
        self.dev.apply_status(make_status(outside_temp=88))
        aux = self.dev._aux["outside"]
        self.assertEqual(aux.values["/DeviceInstance"], 153)
        self.assertEqual(aux.values["/TemperatureType"], 4)  # Outdoor
        self.assertAlmostEqual(aux.values["/Temperature"], 31.1)
        self.assertEqual(aux.values["/Status"], 0)
        self.assertEqual(aux.values["/CustomName"], "Main A/C outside")

    def test_main_service_types(self):
        self.assertEqual(self.dev._temp.values["/TemperatureType"], 3)  # Room

    def test_no_flap_when_only_extra_has_oat(self):
        # Model where status has no outside reading, but ExtraData does.
        ed = et.parse_extra_data({"OAT": 85}, "347")
        self.dev.apply_extra(ed)
        self.assertAlmostEqual(
            self.dev._temp.values["/OutsideTemperature"], 29.4)
        self.assertAlmostEqual(
            self.dev._aux["outside"].values["/Temperature"], 29.4)
        # Status sentinel (None) must NOT clear the extra-sourced value.
        self.dev.apply_status(make_status(outside_temp=None))
        self.assertAlmostEqual(
            self.dev._temp.values["/OutsideTemperature"], 29.4)
        self.assertAlmostEqual(
            self.dev._aux["outside"].values["/Temperature"], 29.4)

    def test_cleared_when_no_source_has_it(self):
        self.dev.apply_status(make_status(outside_temp=88))
        # ExtraData stops reporting it, then status reports sentinel.
        self.dev.apply_extra(et.parse_extra_data({"OAT": 255}, "347"))
        self.dev.apply_status(make_status(outside_temp=None))
        self.assertIsNone(self.dev._temp.values["/OutsideTemperature"])
        self.assertIsNone(self.dev._aux["outside"].values["/Temperature"])
        self.assertEqual(self.dev._aux["outside"].values["/Status"], 4)

    def test_coil_types_generic(self):
        self.dev.apply_extra(et.parse_extra_data({"InCoil": 55}, "347"))
        self.assertEqual(self.dev._aux["incoil"].values["/TemperatureType"], 2)




class TwoThermostatTests(unittest.TestCase):
    """Two physical thermostats coexist with fully isolated services."""

    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.dev1 = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "Main A/C", 0, "", 150,
            serial="352000111")
        self.dev2 = driver.ThermostatDevice(
            self.mgr, "11:22:33:44:55:66", "Bedroom A/C", 0, "", 155,
            serial="347000222")

    def test_distinct_services_and_instances(self):
        self.assertNotEqual(self.dev1._switch.name, self.dev2._switch.name)
        self.assertNotEqual(self.dev1._temp.name, self.dev2._temp.name)
        self.assertEqual(self.dev1._temp.values["/DeviceInstance"], 150)
        self.assertEqual(self.dev2._temp.values["/DeviceInstance"], 155)

    def test_independent_state_and_changes(self):
        self.dev1.apply_config(make_config())
        self.dev2.apply_config(make_config())
        self.dev1.apply_status(make_status(inside_temp=73))
        self.dev2.apply_status(make_status(inside_temp=80, mode=et.MODE_AUTO))
        self.assertAlmostEqual(self.dev1._temp.values["/Temperature"], 22.8)
        self.assertAlmostEqual(self.dev2._temp.values["/Temperature"], 26.7)
        # dev2 write must dispatch under dev2's id only
        self.dev2._switch.external_write("/SwitchableOutput/mode/Dimming", 0)
        for tid, (cb, a) in list(_timers.items()):
            _timers.pop(tid, None); cb(*a)
        self.assertEqual(len(self.mgr.dispatched), 1)
        self.assertEqual(self.mgr.dispatched[0][0], self.dev2.dev_id)

    def test_aux_instances_do_not_collide(self):
        self.dev1.apply_extra(et.parse_extra_data({"InCoil": 55}, "347"))
        self.dev2.apply_extra(et.parse_extra_data({"InCoil": 40}, "347"))
        self.assertEqual(
            self.dev1._aux["incoil"].values["/DeviceInstance"], 151)
        self.assertEqual(
            self.dev2._aux["incoil"].values["/DeviceInstance"], 156)
        self.assertNotEqual(self.dev1._aux["incoil"].name,
                            self.dev2._aux["incoil"].name)

    def test_group_cards_are_separate(self):
        self.dev1.apply_config(make_config())
        self.dev2.apply_config(make_config())
        g1 = self.dev1._switch.values["/SwitchableOutput/mode/Settings/Group"]
        g2 = self.dev2._switch.values["/SwitchableOutput/mode/Settings/Group"]
        self.assertNotEqual(g1, g2)




class AlarmAndRenameTests(unittest.TestCase):
    """Freeze/fault alarms and GUI-persisted renaming."""

    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.dev = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "Main A/C", 0, "", 150,
            serial="347000111")
        self.dev.apply_config(make_config())

    def test_alarms_start_clear(self):
        self.dev.apply_status(make_status())
        t = self.dev._temp.values
        self.assertEqual(t["/Alarms/LowTemperature"], 0)
        self.assertEqual(t["/Alarms/Fault"], 0)

    def test_freeze_fault_raises_warning_not_alarm(self):
        self.dev.apply_status(make_status(fault=5))  # FREEZE DETECTED
        t = self.dev._temp.values
        # warning (1), never an audible alarm (2)
        self.assertEqual(t["/Alarms/LowTemperature"], 1)
        self.assertEqual(t["/Alarms/Fault"], 0)   # freeze not double-reported
        self.assertEqual(t["/Fault"], "FREEZE DETECTED")

    def test_cold_coil_alone_never_warns(self):
        """Coil temperature is reported, not interpreted -- only the unit's
        own FREEZE DETECTED fault raises the warning."""
        self.dev.apply_status(make_status())
        for coil_f in (31, 28, 20, 5):
            self.dev.apply_extra(et.parse_extra_data({"InCoil": coil_f}, "347"))
            self.assertEqual(
                self.dev._temp.values["/Alarms/LowTemperature"], 0,
                "coil %d F must not warn on its own" % coil_f)
        # ...but the coil is still published for custom rules
        self.assertAlmostEqual(
            self.dev._aux["incoil"].values["/Temperature"], -15.0)

    def test_device_freeze_fault_warns_immediately(self):
        """The unit's own trip point is authoritative -- no dwell needed."""
        self.dev.apply_status(make_status(fault=5))
        self.assertEqual(self.dev._temp.values["/Alarms/LowTemperature"], 1)
        self.dev.apply_status(make_status(fault=0))
        self.assertEqual(self.dev._temp.values["/Alarms/LowTemperature"], 0)

    def test_fault_severity_mapping(self):
        t = self.dev._temp.values
        # sensor faults and boondocking conditions -> warning
        for code in (2, 3, 4, 6, 7, 9, 10, 11, 12):
            self.dev.apply_status(make_status(fault=code))
            self.assertEqual(t["/Alarms/Fault"], 1,
                             "fault %d should warn" % code)
        # things that stop the A/C or need service -> alarm
        for code in (1, 8, 13):
            self.dev.apply_status(make_status(fault=code))
            self.assertEqual(t["/Alarms/Fault"], 2,
                             "fault %d should alarm" % code)
        # unknown codes default to alarm
        self.dev.apply_status(make_status(fault=20))
        self.assertEqual(t["/Alarms/Fault"], 2)
        self.assertEqual(t["/Fault"], "UNDEFINED FAULT")
        # clears
        self.dev.apply_status(make_status(fault=0))
        self.assertEqual(t["/Alarms/Fault"], 0)
        self.assertEqual(t["/Alarms/LowTemperature"], 0)

    def test_rename_persists_via_manager(self):
        sw = self.dev._temp
        self.assertTrue(sw.external_write("/CustomName", "Bedroom AC"))
        self.assertEqual(self.mgr.names[(self.dev.dev_id, "main")], "Bedroom AC")
        # a new device object picks the persisted name back up
        dev2 = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "Main A/C", 0, "", 150,
            serial="347000111")
        self.assertEqual(dev2._temp.values["/CustomName"], "Bedroom AC")

    def test_aux_sensor_rename_persists(self):
        self.dev.apply_extra(et.parse_extra_data({"InCoil": 55}, "347"))
        aux = self.dev._aux["incoil"]
        self.assertEqual(aux.values["/CustomName"], "Main A/C in coil")
        self.assertTrue(aux.external_write("/CustomName", "Front coil"))
        self.assertEqual(self.mgr.names[(self.dev.dev_id, "incoil")], "Front coil")




class SetupOutputStatusTests(unittest.TestCase):
    """Setup controls must report Status=On or the GUI shows "Off"
    instead of their value."""

    def test_setup_outputs_status_on(self):
        _timers.clear()
        mgr = FakeManager()
        dev = driver.ThermostatDevice(
            mgr, "AA:BB:CC:DD:EE:FF", "AC Rear", 0, "", 150)
        sw = dev._switch.values
        self.assertEqual(sw["/SwitchableOutput/connection/Status"], 0x09)




class PowerInModeDropdownTests(unittest.TestCase):
    """Master power is folded into the mode dropdown: "Off" powers down,
    any other choice powers on and sets that mode."""

    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.dev = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "AC Front", 0, "", 150)
        self.dev.apply_config(make_config())

    def fire(self):
        for tid, (cb, a) in list(_timers.items()):
            _timers.pop(tid, None); cb(*a)

    def test_off_is_first_entry(self):
        sw = self.dev._switch.values
        self.assertEqual(sw["/SwitchableOutput/mode/Settings/Labels"][0], "Off")

    def test_selecting_off_powers_down(self):
        self.assertTrue(self.dev._switch.external_write(
            "/SwitchableOutput/mode/Dimming", 0))
        self.fire()
        _id, changes = self.mgr.dispatched[0]
        self.assertEqual(changes, {"power": 0})

    def test_selecting_mode_powers_on(self):
        # index 2 == Cool in [Off, Fan Only, Cool, Gas Heat, Auto]
        self.assertTrue(self.dev._switch.external_write(
            "/SwitchableOutput/mode/Dimming", 2))
        self.fire()
        _id, changes = self.mgr.dispatched[0]
        self.assertEqual(changes, {"power": 1, "mode": et.MODE_COOL})

    def test_powered_off_shows_off(self):
        self.dev.apply_status(make_status(system_power=False))
        self.assertEqual(
            self.dev._switch.values["/SwitchableOutput/mode/Dimming"], 0)

    def test_zone_off_mode_shows_off(self):
        self.dev.apply_status(make_status(mode=et.MODE_OFF))
        self.assertEqual(
            self.dev._switch.values["/SwitchableOutput/mode/Dimming"], 0)

    def test_mode_stays_usable_while_off(self):
        self.dev.apply_status(make_status(system_power=False))
        sw = self.dev._switch.values
        self.assertEqual(sw["/SwitchableOutput/mode/Status"], 0x09)
        self.assertEqual(sw["/SwitchableOutput/cool/Status"], 0x20)




class FreezeInputTests(unittest.TestCase):
    """The virtual digital input is what actually reaches VRM's alarm log."""

    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.dev = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "AC Front", 0, "", 150)
        self.dev.apply_config(make_config())

    def test_input_registered(self):
        svc = self.dev._freeze_input
        self.assertIsNotNone(svc)
        self.assertEqual(
            svc.name,
            "com.victronenergy.digitalinput.easytouch_aabbccddeeff_freeze")
        self.assertEqual(svc.values["/DeviceInstance"], 154)
        self.assertEqual(svc.values["/Type"], 10)        # Generic I/O
        self.assertEqual(svc.values["/Alarm"], 0)
        self.assertEqual(svc.values["/CustomName"], "AC Front freeze")

    def test_freeze_fault_raises_input_alarm(self):
        self.dev.apply_status(make_status(fault=5))
        svc = self.dev._freeze_input
        self.assertEqual(svc.values["/Alarm"], 1)   # warning, not alarm
        self.assertEqual(svc.values["/InputState"], 1)
        self.assertEqual(svc.values["/Count"], 1)
        # clears when the fault does
        self.dev.apply_status(make_status(fault=0))
        self.assertEqual(svc.values["/Alarm"], 0)
        self.assertEqual(svc.values["/InputState"], 0)

    def test_count_increments_per_event(self):
        for _ in range(2):
            self.dev.apply_status(make_status(fault=5))
            self.dev.apply_status(make_status(fault=0))
        self.assertEqual(self.dev._freeze_input.values["/Count"], 2)

    def test_alarm_setting_silences_it(self):
        svc = self.dev._freeze_input
        svc.values["/Settings/AlarmSetting"] = 0
        self.dev.apply_status(make_status(fault=5))
        self.assertEqual(svc.values["/Alarm"], 0)
        self.assertEqual(svc.values["/InputState"], 1)   # state still tracks

    def test_other_faults_do_not_raise_it(self):
        self.dev.apply_status(make_status(fault=7))      # NO AC POWER
        self.assertEqual(self.dev._freeze_input.values["/Alarm"], 0)

    def test_can_be_disabled_by_config(self):
        mgr = FakeManager()
        mgr.freeze_input_enabled = False
        dev = driver.ThermostatDevice(
            mgr, "11:22:33:44:55:66", "AC Rear", 0, "", 155)
        self.assertIsNone(dev._freeze_input)
        dev.apply_status(make_status(fault=5))           # must not blow up




class DispatchRateLimitTests(unittest.TestCase):
    """A slow drag must not produce one BLE write per pause."""

    def setUp(self):
        _timers.clear()
        self.mgr = FakeManager()
        self.dev = driver.ThermostatDevice(
            self.mgr, "AA:BB:CC:DD:EE:FF", "AC Front", 0, "", 150)
        self.dev.apply_config(make_config())
        self.dev.apply_status(make_status())
        self.mgr.dispatched.clear()

    def fire(self):
        for tid, (cb, a) in list(_timers.items()):
            _timers.pop(tid, None); cb(*a)

    def test_first_change_dispatches_immediately(self):
        """A fresh device has no previous dispatch, so nothing to wait for."""
        self.assertIsNone(self.dev._last_dispatch_ms)
        self.dev._switch.external_write("/SwitchableOutput/cool/Dimming", 22.0)
        self.fire()
        self.assertEqual(len(self.mgr.dispatched), 1)

    def test_burst_coalesces_to_one_dispatch(self):
        sw = self.dev._switch
        for c in (20.0, 21.0, 22.0):
            sw.external_write("/SwitchableOutput/cool/Dimming", c)
        self.fire()
        self.assertEqual(len(self.mgr.dispatched), 1)
        self.assertEqual(self.mgr.dispatched[0][1], {"cool_sp": 72})

    def test_second_flush_within_window_is_deferred(self):
        sw = self.dev._switch
        sw.external_write("/SwitchableOutput/cool/Dimming", 20.0)
        self.fire()                       # first dispatch goes out
        self.assertEqual(len(self.mgr.dispatched), 1)
        # a further drag step immediately after must not dispatch again;
        # it reschedules instead of hitting the radio
        sw.external_write("/SwitchableOutput/cool/Dimming", 21.0)
        self.fire()
        self.assertEqual(len(self.mgr.dispatched), 1)
        self.assertTrue(self.dev._pending or self.dev._pending_timer is not None)

    def test_deferred_dispatch_carries_latest_value(self):
        sw = self.dev._switch
        sw.external_write("/SwitchableOutput/cool/Dimming", 20.0)
        self.fire()
        for c in (21.0, 22.0, 23.0):      # continued drag while rate-limited
            sw.external_write("/SwitchableOutput/cool/Dimming", c)
            self.fire()
        # pretend the rate-limit window elapsed, then flush
        self.dev._last_dispatch_ms -= driver.MIN_DISPATCH_INTERVAL_MS + 1.0
        self.fire()
        self.assertEqual(len(self.mgr.dispatched), 2)
        self.assertEqual(self.mgr.dispatched[-1][1], {"cool_sp": 73})


if __name__ == "__main__":
    unittest.main(verbosity=2)
