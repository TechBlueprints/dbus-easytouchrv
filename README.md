# dbus-easytouchrv

> **Experimental** -- This project is under active development and has not been
> widely tested. Use at your own risk. The BLE protocol, configuration format,
> and D-Bus interface may change without notice.

Venus OS driver for Micro-Air **EasyTouch RV** thermostats on Victron Cerbo GX
and similar GX devices. Connects to EasyTouch RV air-conditioner / furnace
controllers over Bluetooth Low Energy and exposes each thermostat zone in Venus
OS as a temperature sensor plus a full control card (power, mode, fan,
setpoints) in the GUI v2 switch pane.

## Features

- **BLE Control** -- Reads inside temperature and controls master power, mode,
  fan speed, and cool/heat setpoints over local BLE.
- **Diagnostics** -- Polls the model-specific diagnostics block each cycle:
  coil temperatures (GE, Dometic Single-Zone, Atwood) become their own
  temperature sensors on D-Bus/VRM (freeze watch), supply voltage (GE) and
  relay/cycle state flags (gas assist, electric-heat lockout, defrost, start
  delay) are published on the sensor service.
- **Venus OS Integration** -- Each thermostat registers a
  `com.victronenergy.temperature` sensor (temperature list, VRM) and a
  `com.victronenergy.switch` service whose Switchable Outputs render as one
  card in the GUI v2 switch pane: a power toggle, mode and fan dropdowns, and
  Celsius/Fahrenheit-aware setpoint sliders showing the measured temperature.
  Controls are discovered from the unit itself (available modes, fan speeds,
  and setpoint limits come from its config).
- **Robust BLE Handling** -- A persistent connection per thermostat (the
  vendor app's own model) with ~10 s status updates and instant control, or
  an optional intermittent mode that connects per poll. Cross-process locking,
  adapter rotation, and automatic reconnect with exponential backoff.
- **No Cloud Dependency** -- All control happens over local BLE. No vendor
  account or internet connection is required.

## Supported Devices

Micro-Air EasyTouch RV thermostats running current firmware (protocol
revision > `1.0.5.99`). These controllers advertise a BLE name containing
`EasyTouch` and drive a range of RV rooftop A/C platforms (Dometic, Coleman,
Furrion, Atwood, GE). See [PROTOCOL.md](PROTOCOL.md) for details.

## Requirements

- **Venus OS** >= 3.x on a Cerbo GX (or compatible GX device with Bluetooth)
- **Python 3** (included with Venus OS)
- BLE stack (`bleak` and friends) -- bundled as submodules under `ext/`

## Quick Install

SSH into your Cerbo GX and run:

```bash
curl -fsSL https://raw.githubusercontent.com/TechBlueprints/dbus-easytouchrv/main/install.sh | bash
```

The installer will:
1. Install git if needed
2. Clone the repository to `/data/apps/dbus-easytouchrv`
3. Initialize the bundled submodules
4. Create the service symlink and rc.local entry
5. Start the service

## Configuration

### Automatic Provisioning (recommended)

The included `provision.py` scans BLE for EasyTouch controllers and writes a
ready-to-use `config.ini`:

```bash
cd /data/apps/dbus-easytouchrv
python3 provision.py
```

### Manual Configuration

Copy the default config and add your thermostats:

```bash
cd /data/apps/dbus-easytouchrv
cp config.default.ini config.ini
vi config.ini
```

Add a section per thermostat. The `mac` field must be the BLE advertisement
MAC address (from `python3 easytouch_device.py --scan`):

```ini
[DEFAULT]
connection = persistent
poll_seconds = 10
diagnostics_seconds = 60

[Main_AC]
name = Main A/C
mac = AA:BB:CC:DD:EE:FF
zone = 0
serial = 352123456
passcode =
```

Multiple thermostats are supported -- add one section per unit; each gets
its own persistent connection, GUI cards, and sensor instances. One section
per MAC: duplicate-MAC sections (a multi-zone unit split into per-zone
sections) are not supported and are ignored with a warning.

If your controllers require an account passcode, set `passcode` in `[DEFAULT]`
(applies to all) or per device. Restart the service after editing:

```bash
svc -t /service/dbus-easytouchrv
```

## Usage

Each configured thermostat registers two devices:

- A **temperature sensor** -- the inside temperature appears in the device
  list and on VRM like any other temperature sensor.
- A **switch device** -- the thermostat's controls appear as a card in the
  GUI v2 **switch pane** (and under the device's page):
  - **Power** -- master system power toggle
  - **Mode** -- dropdown listing only the modes the unit supports (Off, Fan
    Only, Cool, Furnace, Auto, ...)
  - **Fan** -- dropdown with the fan choices valid for the current mode
    (Auto / speeds / Off); hidden in modes with a fixed fan
  - **Cool to / Heat to** -- setpoint sliders limited to the unit's setpoint
    range, shown in your configured temperature unit alongside the measured
    inside temperature. In Auto modes they drive the auto setpoints; in Dry
    mode the cool slider drives the dry setpoint.

### Connection model

By default the driver holds a **persistent BLE connection** to each
thermostat -- the same model the vendor app uses -- polling status every
`poll_seconds` (default 10 s) and diagnostics every `diagnostics_seconds`
(default 60 s), so temps, running state, and wall-panel changes appear
near-real-time. Control writes are coalesced for one second, applied
immediately over the held connection, and confirmed with a status
read-back; queued commands survive brief connection drops (and expire
after 2 minutes rather than applying stale).

Because the thermostat accepts only one BLE central, the phone app cannot
connect over Bluetooth while the driver is attached. If the unit is joined
to Wi-Fi the app works through the vendor cloud regardless. If you need
the BLE link free for the app, switch to Intermittent mode -- the driver
then connects, reads, and disconnects each poll interval (control changes
still reconnect immediately).

Both settings are adjustable at runtime from the GUI: the thermostat's card
in the switch pane carries a Persistent/Intermittent dropdown and a
poll-interval slider (5-600 s) alongside its controls. GUI
changes apply immediately (the held connection is dropped or resumed at
the next cycle) and persist across restarts via localsettings, overriding
the config.ini defaults.

### CLI Probe Tool

`easytouch_device.py` can drive a single thermostat from the command line:

```bash
# Scan for EasyTouch devices
python3 easytouch_device.py --scan

# Read status
python3 easytouch_device.py --mac AA:BB:CC:DD:EE:FF --status

# Set mode and cool setpoint
python3 easytouch_device.py --mac AA:BB:CC:DD:EE:FF --mode cool --cool-sp 74

# Turn the thermostat off
python3 easytouch_device.py --mac AA:BB:CC:DD:EE:FF --power off

# Read model-specific diagnostics (coil temps, relay/cycle flags)
python3 easytouch_device.py --mac AA:BB:CC:DD:EE:FF --extra
```

### Tests

Offline unit tests (no BLE or D-Bus required) cover the protocol decoding
and the D-Bus service behaviour:

```bash
python3 tests/test_protocol.py
python3 tests/test_service.py
```

## Service Management

```bash
# View service status
svstat /service/dbus-easytouchrv

# Start / Stop / Restart
svc -u /service/dbus-easytouchrv
svc -d /service/dbus-easytouchrv
svc -t /service/dbus-easytouchrv

# View logs
tail -n 50 /var/log/dbus-easytouchrv/current | tai64nlocal

# Disable (remove from boot)
bash /data/apps/dbus-easytouchrv/disable.sh

# Uninstall completely
bash /data/apps/dbus-easytouchrv/disable.sh
rm -rf /data/apps/dbus-easytouchrv
```

## Architecture

```
dbus-easytouchrv.py         Single-process service
  |  Per zone: com.victronenergy.temperature.easytouch_<id>  (sensor)
  |            com.victronenergy.switch.easytouch_<id>       (controls)
  |  One asyncio monitor task per thermostat owns the BLE connection:
  |  status every ~10 s, diagnostics every ~60 s, queued changes applied
  |  immediately, reconnect with exponential backoff
  |
  +-- easytouch_ble.py       BLE protocol module
  |     JSON-over-GATT command/response, passcode handshake
  |     run_device_monitor supervisor (persistent or transient sessions)
  |     Claim-aware adapter routing via bleak-connection-manager v2's
  |     process-wide bleak catcher; retries via bleak-retry-connector
  |     get_status / get_config / change / ExtraData; capability decoding
  |
  +-- easytouch_device.py    Standalone CLI probe tool
  |
  +-- provision.py           Provisioning tool (scans BLE, writes config.ini)
```

### D-Bus Paths

Temperature service (`com.victronenergy.temperature.easytouch_<id>`):

| Path | Access | Description |
|------|--------|-------------|
| `/Temperature` | read | Inside temperature (°C) |
| `/OutsideTemperature` | read | Outside air temperature (°C, if sensed) |
| `/FaceplateTemperature` | read | Faceplate sensor (°C) |
| `/RoomSensorTemperature` | read | Remote room sensor (°C, Atwood) |
| `/WeatherTemperature` | read | Cloud weather temp (°C, Wi-Fi units only) |
| `/Voltage` | read | Supply voltage (V, GE) |
| `/DiagnosticFlags` | read | Active state flags, comma-separated (e.g. `cooling,gas_assist`) |
| `/ModeName` | read | Human-readable mode |
| `/RunningState` | read | idle / cooling / heating |
| `/Fault` | read | Fault text (empty if none) |
| `/Status` | read | Sensor connection status (0 = OK, 4 = disconnected) |
| `/FirmwareVersion` | read | Thermostat firmware revision |

Up to four temperatures register as first-class sensor services (visible in
the device list and on VRM), created on the first valid reading:

| Service suffix | Sensor | `/TemperatureType` | Updated |
|----------------|--------|--------------------|---------|
| *(main)* | Inside temperature | 3 (Room) | every status poll |
| `_outside` | Outside air | 4 (Outdoor) | every status poll (diagnostics fallback on models where status lacks it) |
| `_incoil` | Inside (freeze) coil | 2 (Generic) | every diagnostics poll |
| `_outcoil` | Outside coil | 2 (Generic) | every diagnostics poll |

Coil availability depends on the A/C platform (GE, Dometic Single-Zone, and
Atwood report them). Each thermostat reserves a block of 5 device instances
(main +1 in-coil, +2 out-coil, +3 outside), so instances stay stable across
restarts.

Switch service (`com.victronenergy.switch.easytouch_<id>`), standard Venus OS
Switchable Output API under `/SwitchableOutput/<output>/...`:

| Output | Type | Value path | Meaning |
|--------|------|------------|---------|
| `power` | 1 (toggle) | `State` 0/1 | Master system power |
| `mode` | 6 (dropdown) | `Dimming` = index into `Settings/Labels` | Operating mode |
| `fan` | 6 (dropdown) | `Dimming` = index into `Settings/Labels` | Fan for the current mode |
| `cool` | 3 (temp. setpoint) | `Dimming` °C | Cool / dry / auto-cool setpoint |
| `heat` | 3 (temp. setpoint) | `Dimming` °C | Heat / auto-heat setpoint |

Sliders carry `Settings/DimmingMin`/`DimmingMax` from the unit's setpoint
limits and `Measurement` (inside temperature) for the GUI's combined
"setpoint / measured" display. All temperatures on D-Bus are Celsius; the
device itself operates in integer Fahrenheit and values snap accordingly.

## Troubleshooting

### Device not discovered

- Ensure the thermostat is powered and in BLE range
- Check that the BLE adapter is working: `hciconfig hci0`
- Verify the device appears: `python3 easytouch_device.py --scan`

### Control writes have no effect

- If your controller requires a passcode, confirm it is set in `config.ini`
- Check logs: `tail -n 50 /var/log/dbus-easytouchrv/current | tai64nlocal`

## License

Apache License 2.0. See [LICENSE](LICENSE) for details.
