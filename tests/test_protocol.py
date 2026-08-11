"""Offline logic tests for dbus-easytouchrv (no BLE, no D-Bus).

Stubs bleak/bleak_connection_manager so easytouch_ble imports cleanly on
the dev machine, then exercises the pure protocol/UI-mapping helpers.
"""
import os
import sys

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
import sys
import types
import unittest

# Stub the BLE runtime deps before importing the module under test.
for name in ("bleak",):
    mod = types.ModuleType(name)
    mod.BleakClient = object
    sys.modules.setdefault(name, mod)

bcm = types.ModuleType("bleak_connection_manager")
for sym in ("EscalationConfig", "EscalationPolicy", "LockConfig", "ScanLockConfig"):
    setattr(bcm, sym, type(sym, (), {"__init__": lambda self, *a, **k: None}))
for fn in ("establish_connection", "discover_adapters", "managed_discover",
           "managed_find_device", "validate_char_exists"):
    setattr(bcm, fn, lambda *a, **k: None)
sys.modules.setdefault("bleak_connection_manager", bcm)

sys.path.insert(0, _REPO_ROOT)

import easytouch_ble as et  # noqa: E402


class FanTests(unittest.TestCase):
    def test_fixed_speed_no_options(self):
        self.assertEqual(et.fan_options(0x10 | 3), [])

    def test_full_options(self):
        # full auto + range auto + off allowed, 3 speeds
        opts = et.fan_options(0x80 | 0x40 | 0x20 | 3)
        self.assertEqual(opts, [
            ("Auto", 0x80),
            ("Auto Low", 0x41), ("Auto Medium", 0x42), ("Auto High", 0x43),
            ("Low", 1), ("Medium", 2), ("High", 3),
            ("Off", 0)])

    def test_cycled_auto_offered_per_speed(self):
        """GE units advertise range-auto but no full auto: every cycled
        speed must be selectable, or "Auto" silently means cycled-at-low."""
        opts = et.fan_options(66)          # real FA[2] from a GE 347 unit
        self.assertEqual(opts, [("Auto Low", 0x41), ("Auto High", 0x42),
                                ("Low", 1), ("High", 2)])
        for raw, expect in ((0x41, "Auto Low"), (0x42, "Auto High"),
                            (1, "Low"), (2, "High")):
            self.assertEqual(opts[et.fan_option_index(opts, raw)][0], expect)

    def test_no_off(self):
        opts = et.fan_options(0x80 | 2)
        self.assertEqual(opts, [("Auto", 0x80), ("Low", 1), ("High", 2)])

    def test_single_speed(self):
        opts = et.fan_options(0x20 | 1)
        self.assertEqual(opts, [("On", 1), ("Off", 0)])

    def test_option_index(self):
        opts = et.fan_options(0x80 | 0x20 | 3)  # Auto, Low, Med, High, Off
        self.assertEqual(et.fan_option_index(opts, 0x80), 0)   # full auto
        # no cycled entries here (0x40 not allowed): falls back to the speed
        self.assertEqual(opts[et.fan_option_index(opts, 0x41)][0], "Low")
        self.assertEqual(opts[et.fan_option_index(opts, 2)][0], "Medium")
        self.assertEqual(opts[et.fan_option_index(opts, 0)][0], "Off")

    def test_change_keys(self):
        self.assertEqual(et.fan_change_key(et.MODE_FAN_ONLY), "fanOnly")
        self.assertEqual(et.fan_change_key(et.MODE_COOL), "coolFan")
        self.assertEqual(et.fan_change_key(et.MODE_FURNACE), "gasFan")
        self.assertEqual(et.fan_change_key(et.MODE_HEAT_PUMP), "eleFan")
        self.assertEqual(et.fan_change_key(et.MODE_AUTO), "autoFan")
        self.assertIsNone(et.fan_change_key(et.MODE_DRY))
        self.assertIsNone(et.fan_change_key(et.MODE_OFF))


class StatusParseTests(unittest.TestCase):
    STATUS = {
        "Type": "Response", "RT": "Status", "REV": "1.0.6.5",
        "PRM": [0, 8, 78, 72],
        "Z_sts": {"0": [70, 76, 74, 68, 72, 50, 1, 0x81, 2, 0x80, 2, 3, 73, 88, 0, 2]},
        "alertUL": 100, "alertLL": 40,
    }

    def test_parse(self):
        zones = et._parse_status(self.STATUS)
        st = zones[0]
        self.assertEqual(st.mode, et.MODE_COOL)
        self.assertEqual(st.inside_temp, 73)
        self.assertEqual(st.cool_sp, 74)
        self.assertEqual(st.heat_sp, 68)
        self.assertEqual(st.auto_heat_sp, 70)
        self.assertEqual(st.auto_cool_sp, 76)
        self.assertTrue(st.system_power)
        self.assertTrue(st.is_cooling)
        self.assertEqual(st.rev, "1.0.6.5")
        # fan slots
        self.assertEqual(st.fan_fanonly, 1)
        self.assertEqual(st.fan_cool, 0x81)
        self.assertEqual(st.fan_elec, 2)
        self.assertEqual(st.fan_auto, 0x80)
        self.assertEqual(st.fan_gas, 3)
        # current mode is Cool -> cool fan slot
        self.assertEqual(st.fan_value(), 0x81)
        self.assertEqual(st.fan_value(et.MODE_FURNACE), 3)
        self.assertEqual(st.fan_value(et.MODE_AUTO), 0x80)
        self.assertEqual(st.fan_value(et.MODE_HEAT_STRIP), 2)

    def test_outside_sensor(self):
        zones = et._parse_status(self.STATUS)
        self.assertEqual(zones[0].outside_temp, 88)
        bad = dict(self.STATUS)
        arr = list(bad["Z_sts"]["0"])
        arr[13] = 255  # >= 160 -> no sensor
        bad["Z_sts"] = {"0": arr}
        self.assertIsNone(et._parse_status(bad)[0].outside_temp)

    def test_config_parse(self):
        cfg_obj = {
            "Type": "Response", "RT": "Config",
            "CFG": '{"Zone":0,"MAV":263,"SPL":[65,85,45,80],'
                   '"MA":[1,1,1,1,0,0,0,0,1,0,0,0,0,0,0,0],'
                   '"FA":[0,163,163,17,17,0,0,0,163,0,0,0,0,0,0,0]}',
        }
        cfg = et._parse_config(cfg_obj)
        self.assertEqual(cfg.zone, 0)
        self.assertEqual(cfg.available_modes, 263)
        self.assertTrue(cfg.mode_available(0))
        self.assertTrue(cfg.mode_available(1))
        self.assertTrue(cfg.mode_available(2))
        self.assertTrue(cfg.mode_available(8))
        self.assertFalse(cfg.mode_available(4))
        self.assertEqual((cfg.min_cool, cfg.max_cool), (65, 85))
        self.assertEqual((cfg.min_heat, cfg.max_heat), (45, 80))
        # FA[2] = 163 = 0x80|0x20|3 -> Auto, 3 speeds, Off
        opts = et.fan_options(cfg.fan_array[2])
        self.assertEqual([l for l, _ in opts], ["Auto", "Low", "Medium", "High", "Off"])
        # FA[3] = 17 = fixed speed -> no control
        self.assertEqual(et.fan_options(cfg.fan_array[3]), [])

    def test_unused_zone_config(self):
        cfg = et._parse_config({"CFG": '{"Zone":2}'})
        self.assertEqual(cfg.available_modes, 0)


class TempConversionTests(unittest.TestCase):
    def setUp(self):
        # import the conversion helpers from the service module without
        # importing dbus/GLib: extract them by exec'ing the defs is overkill;
        # replicate the formulas and check round-trip stability instead.
        self.f_to_c = lambda f: round((f - 32.0) * 5.0 / 9.0, 1)
        self.c_to_f = lambda c: int(round(c * 9.0 / 5.0 + 32.0))

    def test_round_trip_stable(self):
        for f in range(40, 111):
            c = self.f_to_c(f)
            self.assertEqual(self.c_to_f(c), f,
                             "round-trip failed for %d F (%.1f C)" % (f, c))


class ExtraDataTests(unittest.TestCase):
    def test_ge_347(self):
        ed = et.parse_extra_data(
            {"Zone": 0, "SS": 2, "OAT": 85, "InCoil": 55, "OutCoil": 95,
             "VDC": 1342}, "347")
        self.assertEqual(ed.outside_air, 85)
        self.assertEqual(ed.inside_coil, 55)
        self.assertEqual(ed.outside_coil, 95)
        self.assertAlmostEqual(ed.vdc, 13.42)
        self.assertTrue(ed.flags["remote_sensor"])

    def test_ge_347_sentinels(self):
        ed = et.parse_extra_data(
            {"OAT": 255, "InCoil": -70, "OutCoil": 200, "VDC": 2500}, "347")
        self.assertIsNone(ed.outside_air)   # 255 = bad sensor
        self.assertIsNone(ed.inside_coil)   # < -60 = not used
        self.assertIsNone(ed.outside_coil)  # > 160 = not used
        self.assertIsNone(ed.vdc)           # 25 V out of range

    def test_dometic_350(self):
        ed = et.parse_extra_data(
            {"SS": 0x1B, "Output": 0x20, "OAT": 0}, "350")
        self.assertIsNone(ed.outside_air)   # 0 = none on 350
        self.assertTrue(ed.flags["defrost"])
        self.assertTrue(ed.flags["remote_sensor"])
        self.assertFalse(ed.flags["eh_override_available"])
        self.assertTrue(ed.flags["eh_override"])
        self.assertTrue(ed.flags["eh_lockout"])
        self.assertTrue(ed.flags["cycle_delay"])

    def test_dometic_351(self):
        ed = et.parse_extra_data({"FS": 34, "OAT": 70, "GCE": 1, "SS": 0x203}, "351")
        self.assertEqual(ed.inside_coil, 34)
        self.assertEqual(ed.outside_air, 70)
        self.assertTrue(ed.flags["remote_sensor"])
        self.assertTrue(ed.flags["gas_override_enabled"])
        self.assertTrue(ed.flags["gas_override"])
        self.assertTrue(ed.flags["defrost"])

    def test_dometic_352(self):
        ed = et.parse_extra_data({"OS": 0x4A, "SS": 0x30}, "352")
        self.assertTrue(ed.flags["cooling"])
        self.assertTrue(ed.flags["gas_assist"])
        self.assertTrue(ed.flags["start_delay"])
        self.assertFalse(ed.flags["remote_sensor"])  # 0x10 = internal
        self.assertTrue(ed.flags["gas_assist_enabled"])

    def test_coleman_355(self):
        ed = et.parse_extra_data({"SS": 0x200 | 0x10 | 0x04 | 0x01 | 0x80}, "355")
        self.assertFalse(ed.flags["remote_sensor"])  # 0x200 = internal here
        self.assertTrue(ed.flags["gas_relay"])
        self.assertTrue(ed.flags["gas_assist"])
        self.assertTrue(ed.flags["high_heat"])

    def test_coleman_356(self):
        ed = et.parse_extra_data({"SS": 0x02, "OS": 0x04}, "356")
        self.assertTrue(ed.flags["eh_lockout"])
        self.assertTrue(ed.flags["heating"])
        self.assertTrue(ed.flags["remote_sensor"])  # 0x10 clear = remote

    def test_atwood_358(self):
        ed = et.parse_extra_data(
            {"useFPS": 1, "fPlate": 75, "RMSen": 205, "InCoil": 40,
             "OutCoil": 90, "OAT": 210}, "358")
        self.assertFalse(ed.flags["remote_sensor"])
        self.assertEqual(ed.faceplate, 75)
        self.assertIsNone(ed.room_sensor)   # >= 200 invalid
        self.assertEqual(ed.inside_coil, 40)
        self.assertIsNone(ed.outside_air)

    def test_unknown_prefix(self):
        ed = et.parse_extra_data({"Zone": 1, "Whatever": 5}, "")
        self.assertEqual(ed.zone, 1)
        self.assertEqual(ed.flags, {})
        self.assertIsNone(ed.inside_coil)

    def test_model_prefix_from_name(self):
        self.assertEqual(et.model_prefix_from_name("EasyTouch 352123456"), "352")
        self.assertEqual(et.model_prefix_from_name("My AC"), "")
        self.assertEqual(et.model_prefix_from_name(""), "")


class PrmExtrasTests(unittest.TestCase):
    def test_weather_and_faceplate(self):
        obj = {"PRM": [0, 8, 78, 72],
               "Z_sts": {"0": [70,76,74,68,72,50,1,1,2,1,2,3,73,88,0,2]}}
        st = et._parse_status(obj)[0]
        self.assertEqual(st.weather_temp, 78)
        self.assertEqual(st.faceplate_temp, 72)

    def test_weather_sentinel(self):
        obj = {"PRM": [0, 8, 255, 72],
               "Z_sts": {"0": [70,76,74,68,72,50,1,1,2,1,2,3,73,88,0,2]}}
        st = et._parse_status(obj)[0]
        self.assertIsNone(st.weather_temp)


if __name__ == "__main__":
    unittest.main(verbosity=2)
