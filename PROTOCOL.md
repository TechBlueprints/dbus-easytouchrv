# EasyTouch RV Thermostat Protocol Reference

## Overview

Micro-Air EasyTouch RV thermostats are BLE-connected RV air-conditioner /
furnace controllers. They expose a single custom GATT service and speak a
simple **line-oriented JSON protocol**: the client writes a JSON command
object to a command characteristic and reads a JSON response object from a
return characteristic.

There is no binary framing, no checksum, and no sequence numbering. Each
message is a self-contained UTF-8 JSON object with a top-level `Type`
discriminator. The protocol is transport-agnostic — the same JSON messages
are used over BLE and (optionally) over the vendor cloud — but this driver
uses **BLE only**.

## Supported Devices

EasyTouch RV controllers advertise a BLE name containing `EasyTouch`, followed
by a 9-digit serial number, e.g. `EasyTouch 352123456`. The first three digits
of the serial identify the air-conditioner model the unit is paired with:

| Prefix | A/C platform |
|--------|--------------|
| 347 | GE |
| 350 | Dometic CCC2 |
| 351 | Dometic Single-Zone |
| 352 | Dometic |
| 353 | Furrion |
| 354 | Coleman 8000 series |
| 355 | Coleman 9000 series |
| 356 | Coleman two-stage |
| 357 | Dometic CCC1 |
| 358 | Atwood |
| 359 | Dometic |

All current-firmware units use the **unified protocol** described below,
regardless of A/C platform. The A/C model only affects which modes and fan
speeds the unit reports as available (see *Config* below).

## Firmware Revisions

Two protocol generations exist, distinguished by the `REV` string returned in
a status response (a dotted `major.minor.patch.build` version):

- **`REV` > `1.0.5.99`** — the unified protocol (this document). Capabilities
  are discovered at runtime via a `Get Config` request; status is returned as
  a fixed-layout per-zone array. **This driver targets the unified protocol.**
- **`REV` <= `1.0.5`** — a legacy protocol with per-model status layouts. Not
  supported by this driver.

## BLE GATT Profile

The controller exposes one vendor service built on 16-bit short UUIDs over the
standard Bluetooth base UUID (`0000xxxx-0000-1000-8000-00805F9B34FB`).

| Role | UUID | Short | Access |
|------|------|-------|--------|
| Vendor service | `000000FF-0000-1000-8000-00805F9B34FB` | `0xFF` | — |
| JSON command | `0000EE01-0000-1000-8000-00805F9B34FB` | `0xEE01` | Write |
| JSON return | `0000FF01-0000-1000-8000-00805F9B34FB` | `0xFF01` | Read (+ Notify) |
| Password | `0000DD01-0000-1000-8000-00805F9B34FB` | `0xDD01` | Write + Read |

### Message exchange

1. **Write** a UTF-8 JSON command string to `0xEE01`.
2. **Read** `0xFF01` to retrieve the UTF-8 JSON response string.

The unit also *supports* pushing responses as notifications on `0xFF01`, but a
poll-after-write (read `0xFF01` shortly after writing `0xEE01`) is the reliable
pattern and does not depend on notifications being enabled.

### MTU / write type (verified on hardware)

Command payloads routinely exceed the 20-byte default ATT payload (a typical
`Get Status` is ~51 bytes), but the unit does **not** negotiate a larger MTU
on BlueZ — it stays at 23 (20 usable). Despite this:

- **Commands must be written *without* response** (ATT Write Command). The
  full payload is accepted in one operation even though it exceeds the MTU.
- **Write-with-response fails.** BlueZ turns an over-MTU write-with-response
  into a prepared/long write, which the firmware rejects with GATT
  `Unlikely Error` (0x0E) and then drops the connection. This happens even
  for a 20-byte chunk, so chunking does not help.
- The same applies to the passcode characteristic (`0xDD01`).
- `0xFF01` is **read-only** (no notify); `0xEE01` advertises notify. Poll
  `0xFF01` with a read after writing the command.

Responses can exceed the MTU, so implementations must not assume a single
BLE packet holds a whole message — buffer a full read before parsing.

## Authentication

Immediately after service discovery the client writes an account passcode as
raw UTF-8 bytes to the password characteristic (`0xDD01`), then reads `0xDD01`
back. The unit replies with a short status string:

| Response contains | Meaning |
|-------------------|---------|
| `Matched` | Passcode accepted — commands will be honoured |
| `Invalid` | Passcode rejected |

There is no BLE bonding, PIN pairing, or link encryption.

**The handshake is mandatory in practice** (verified on hardware): a unit
tied to a Micro-Air account replies `Invalid` to an empty/incorrect
passcode, then silently ignores the next command and **drops the BLE
connection**. A command write that "succeeds" followed by an immediate
disconnect is the signature of a failed passcode. The passcode is the
Micro-Air account password and must be written after every connection,
before any command.

## Message Types

Every message is a JSON object with a `Type` field.

### `Get Status` (client → unit)

Requests the current state of a zone.

```json
{"Type":"Get Status","Zone":0,"TM":1720800000}
```

| Field | Type | Description |
|-------|------|-------------|
| `Type` | string | `"Get Status"` |
| `Zone` | int | Zone index to query (0-based) |
| `TM` | int | Client Unix time (seconds), used to sync the unit clock |

Optional fields the app sends on the first few requests to set the unit's
clock and location: `TZ` (string, e.g. `"EST+5"`), `LAT`/`LON` (strings,
decimal degrees), `DST` (int, minutes). These are not required for control.

### `Get Config` (client → unit)

Requests the static capabilities of a zone. Sent once per zone at startup.

```json
{"Type":"Get Config","Zone":0}
```

### `Change` (client → unit)

Applies one or more state changes to a zone. Multiple changes are batched into
a single `Changes` object.

```json
{"Type":"Change","Changes":{"zone":0,"mode":2,"cool_sp":74}}
```

The `Changes` object always carries `zone` (the target zone id) plus one or
more of the change keys below.

| Key | Type | Meaning |
|-----|------|---------|
| `mode` | int | Operating mode (see *Mode values*) |
| `power` | int | Master power: `1` = on, `0` = off |
| `cool_sp` | int | Cool setpoint, °F |
| `heat_sp` | int | Heat setpoint, °F |
| `dry_sp` | int | Dry-mode setpoint, °F |
| `autoCool_sp` | int | Auto-mode cool setpoint, °F |
| `autoHeat_sp` | int | Auto-mode heat setpoint, °F |
| `fanOnly` | int | Fan value for Fan-Only mode (see *Fan encoding*) |
| `coolFan` | int | Fan value for Cool mode |
| `gasFan` | int | Fan value for gas/furnace heat |
| `eleFan` | int | Fan value for electric heat |
| `autoFan` | int | Fan value for auto modes |

When changing setpoints, the app sends all five setpoint keys together; the
unit applies whichever are relevant to the active mode.

`{"null":0}` is accepted as a no-op change (the vendor app sends it as a
keep-alive when the user switches the displayed zone).

#### Global settings keys

These additional `Changes` keys target unit-wide settings rather than zone
state; the app always sends them with `"zone":0`:

| Key | Type | Meaning |
|-----|------|---------|
| `push` | int | Push notifications: `1` = enabled, `0` = disabled |
| `alertUL` | int | Notification upper temperature limit, °F (max 110) |
| `alertLL` | int | Notification lower temperature limit, °F (min 40; kept >= 2 °F below `alertUL`) |
| `hA` | int | Home/Away: `1` = away settings active, `0` = home |
| `clockFlags` | int | Clock flag bitfield; bit `0x02` = schedule enabled. Sent together with `Schedule` |
| `Schedule` | int | Schedule enable: `1` = on, `0` = off |
| `cal` | string | `"OK"` triggers a touchscreen recalibration |
| `reset` | string | `"OK"` reboots the control (app label: "Reset Thermostat") |
| `SSID` / `PWD` | string | Wi-Fi provisioning credentials (sent together, without a `zone` key) |

## Response Format

Responses have `Type` `"Response"` with a `RT` (response-type) discriminator.

| `RT` | Meaning |
|------|---------|
| `Status` | Full zone status (below) |
| `Config` | Zone capabilities (below) |
| `OK` | A `Change` was accepted |

### Status response

```json
{
  "Type":"Response","RT":"Status","REV":"1.0.6.x",
  "PRM":[0, 8, 78, 72],
  "Z_sts":{"0":[70,76,74,68,72,50,0,3,0,131,2,3,73,88,0,2]},
  "alertUL":100,"alertLL":40
}
```

**`PRM`** — global parameters array:

| Index | Meaning |
|-------|---------|
| 0 | Currently displayed zone |
| 1 | Global flag bitfield (below) |
| 2 | Weather / outside temperature, °F |
| 3 | Faceplate temperature, °F |

`PRM[1]` flag bits: `0x01` wifi connected, `0x02` cloud connected, `0x04` push
notifications, `0x08` **system power (master on/off)**, `0x10` schedule
enabled, `0x20` aqua icon, `0x40` away settings.

**`Z_sts`** — object keyed by zone-id string; each value is a fixed 16-element
array:

| Index | Field | Units / meaning |
|-------|-------|-----------------|
| 0 | autoHeat setpoint | °F |
| 1 | autoCool setpoint | °F |
| 2 | cool setpoint | °F |
| 3 | heat setpoint | °F |
| 4 | dry setpoint | °F |
| 5 | humidity setpoint | %RH |
| 6 | fan value — Fan-Only mode | see *Fan encoding* |
| 7 | fan value — Cool mode | see *Fan encoding* |
| 8 | fan value — electric heat | see *Fan encoding* |
| 9 | fan value — auto modes | see *Fan encoding* |
| 10 | **mode** | see *Mode values* |
| 11 | fan value — gas/furnace heat | see *Fan encoding* |
| 12 | **inside temperature** | °F |
| 13 | outside air temperature | °F; `<= -60` or `>= 160` means "no sensor" |
| 14 | **fault code** | see *Fault codes* |
| 15 | **activity bitfield** | below |

`Z_sts[..][15]` activity bits: `0x01` cycle active, `0x02` cooling, `0x04`
heating, `0x08` electric-heat override, `0x10` electric-heat lockout, `0x20`
auto-heat.

### Config response

```json
{"Type":"Response","RT":"Config","CFG":"{...zone config json...}"}
```

`CFG` is a stringified JSON object:

| Key | Type | Meaning |
|-----|------|---------|
| `Zone` | int | Zone id |
| `MAV` | int | Available-modes bitmask — mode `i` is available if `MAV & (1<<i)` |
| `SPL` | int[4] | `[minCool, maxCool, minHeat, maxHeat]` setpoint limits, °F |
| `MA` | int[16] | Per-mode capability array |
| `FA` | int[16] | Per-mode fan array; low nibble is that mode's max fan speed, upper bits are capability flags (see *FA capability bits*) |

An unused zone's `CFG` contains only `Zone` (no `MAV`/`SPL`/`MA`/`FA`) and
should be treated as absent.

## Other Message Types

Beyond `Get Status` / `Get Config` / `Change`, the protocol has several more
message types. The driver uses `ExtraData` (diagnostics); the schedule,
Wi-Fi-provisioning, and firmware messages are documented for completeness
only.

### Schedules

`{"Type":"Get Schedule","Day":d}` (`d` = 0..6, Sunday-based) returns
`{"Type":"Response","RT":"Schedule","DAY":d,"EVNT":{...}}` with that day's
events. A day's schedule is written back as a flat array, five ints per event:

```json
{"Type":"Change","day":0,"data":[t,heatSP,coolSP,modeZones,fan, ...]}
```

- `t` — packed time: bit `0x80` = event disabled, bits 6..2 = hour (0–23),
  bits 1..0 = quarter-hour (`00/15/30/45`)
- `modeZones` — low nibble mode, high nibble zone bitmask (`mode | zones<<4`)
- `fan` — fan value (see *Fan encoding*)

`{"Type":"Get Modes"}` returns the modes valid for scheduling.

### Wi-Fi provisioning

`{"Type":"Get SSIDs","Action":0}` starts a network scan (`Action:1` polls for
results). Responses have `RT:"SSID"` and progress through `START` /
`RUNNING` markers to a final `{"count":N,"entries":{"SSIDs":[...]}}`.
Credentials are then written with the `SSID`/`PWD` change keys.

### Diagnostics (`ExtraData`) — coil and sensor temperatures

`{"Type":"ExtraData","Zone":z}` returns
`{"Type":"Response","RT":"ExtraData", ...}` with a **model-specific** field
set. This is where the coil/sensor temperatures and relay/cycle details
live. All temperatures are integer °F.

Temperature validity sentinels (varies by model):

- `255` — bad sensor
- outside `-60..160` — sensor not used / not connected
- `>= 200` — not valid (Atwood 358)
- `0` — treated as "no sensor" on Dometic 350/351

Fields by A/C model prefix:

| Prefix | Fields |
|--------|--------|
| 347 GE | `OAT` outside air °F; `InCoil` / `OutCoil` coil temps °F; `VDC` supply voltage ×100 (valid 8–20 V); `SS` bit `0x02` = remote inside sensor in use |
| 350 Dometic CCC2 | `OAT` °F; `Output` bit `0x20` = cycle delay; `SS` bits: `0x01` defrost active, `0x02` remote inside sensor, `0x04` electric-heat override available, `0x08` EH override active, `0x10` EH lockout active |
| 351 Dometic SZ | `FS` inside-coil (freeze) sensor °F; `OAT` °F; `GCE` bit `0x01` = gas override enabled; `SS` bits: `0x200` remote sensor, `0x01` gas override active, `0x02` gas lockout / defrost active |
| 352 / 359 Dometic | `OS` bits: `0x01` active, `0x02` cooling, `0x04` heating, `0x08` gas assist active, `0x10` EH lockout, `0x40` start delay; `SS` bits: `0x10` internal sensor, `0x20` gas assist enabled |
| 354 / 355 Coleman | `SS` bits: `0x200` internal sensor, `0x10` gas relay on, `0x08` gas-only lockout, `0x04` gas assist enabled, `0x01` high heat available, `0x80` high heat active |
| 356 Coleman two-stage | `SS` bits: `0x10` internal sensor, `0x01` EH active, `0x02` EH lockout, `0x04` EH disabled, `0x08` EH inactive; `OS` bits: `0x01` active, `0x02` cooling, `0x04` heating, `0x40` starting soon |
| 358 Atwood | `useFPS` > 0 = internal (faceplate) sensor; `fPlate`, `RMSen` (room sensor), `InCoil`, `OutCoil`, `OAT` — all °F, invalid when `>= 200` |

Notes:

- Only the GE, Dometic SZ, and Atwood platforms report coil temperatures;
  the Coleman and Dometic CCC/352/359 blocks are relay/cycle state only.
- The internal-vs-remote sensor bit is not consistent across models (`0x200`
  set means *remote* on 351 but *internal* on 354/355; 352/356 use `0x10`
  for internal).
- The vendor app polls `ExtraData` on the same 4 s cadence as status while
  its per-model info screen is open.

### Firmware / pairing

- `{"Type":"UPDATE","Action":...,"Device Type":...,"Serial":...,"Revision":...,
  "Web Server":...,"Web URL":...,"Port":...}` — triggers a firmware download
  from the vendor's server (parameters come from the vendor cloud).
- `Pair` — message type observed in the app's receive handler (cloud pairing).

## Mode Values

The `mode` field (status index 10) and the `mode` change key use:

| Value | Mode |
|-------|------|
| 0 | Off |
| 1 | Fan only |
| 2 | Cool |
| 3 | Gas heat / aqua |
| 4 | Furnace (gas heat) |
| 5 | Heat pump |
| 6 | Dry |
| 7 | Heat strip |
| 8 | Auto |
| 9 | Auto heat strip |
| 10 | Auto heat pump |
| 11 | Auto furnace |
| 12 | Electric heat |
| 16 | Zone off |

Heat-type modes (show heat setpoint): 3, 4, 5, 7, 12. Cool/Dry show the
cool/dry setpoint. Auto modes (8–11) show both auto setpoints and enforce a
minimum 2 °F deadband between them.

## Fan Encoding

Fan values (both the per-mode `*Fan` change keys and the status fan slots) pack
an auto flag and a discrete speed:

| Bit / value | Meaning |
|-------------|--------|
| `0x80` | Full auto ("Fan Auto") |
| `0x40` | Auto within manual range (encoded as `0x40 \| speed`, e.g. `0x41`) |
| low nibble `0x0F` | Discrete speed, `1..N` (N = `FA[mode] & 0x0F`) |
| `0x00` | Fan off |

The fan gauge fullness is `(value & 0x0F) / (FA[mode] & 0x0F)`. Before writing
a fan value the app masks it with `0xCF` (bits `0x10`/`0x20` are never sent).

### `FA` capability bits

Each entry of the config `FA` array carries capability flags for that mode in
its upper bits, alongside the max speed in the low nibble:

| Bit | Meaning |
|-----|---------|
| `0x80` | Full auto allowed |
| `0x40` | Manual-range auto allowed |
| `0x20` | Fan off allowed |
| `0x10` | Fixed speed (fan locked to max; no speed selection) |
| `0x0F` | Maximum discrete speed N |

The app's fan cycle order is: manual `1..N` → manual-auto (`0x40|1`, if
allowed) → full auto (`0x80`, if allowed) → off (`0x00`, if allowed) → back to
manual `1`.

### Which fan slot applies per mode

| Mode | Fan change key | Status fan slot |
|------|----------------|-----------------|
| 1 (Fan only) | `fanOnly` | index 6 |
| 2 (Cool) | `coolFan` | index 7 |
| 3, 5, 7, 12 (heat / heat pump / heat strip / electric) | `eleFan` | index 8 |
| 4 (Furnace) | `gasFan` | index 11 |
| 8–11 (Auto modes) | `autoFan` | index 9 |
| 6 (Dry) | — (no fan control in this mode) | — |

## Fault Codes

Status index 14:

| Code | Meaning |
|------|---------|
| 0 | (no fault) |
| 1 | NO COMMUNICATION |
| 2 | BAD REMOTE SENSOR |
| 3 | BAD OUTSIDE SENSOR |
| 4 | BAD FREEZE SENSOR |
| 5 | FREEZE DETECTED |
| 6 | BAD HUMIDITY SENSOR |
| 7 | NO AC POWER |
| 8 | INVALID CONFIG |
| 9 | LOW DC VOLTAGE |
| 10 | BAD INDOOR SENSOR |
| 11 | INSIDE COIL SENSOR |
| 12 | OUTSIDE COIL SENSOR |
| 13 | LOW REFRIGERANT |
| >= 14 | UNDEFINED FAULT |

## Temperature Units

All temperatures and setpoints on the wire are integer **degrees Fahrenheit**.
Celsius is a display conversion only: `C = (F - 32) * 5 / 9`.

## Connection Sequence

1. **Scan** for BLE devices whose name contains `EasyTouch`.
2. **Connect** via GATT (no pairing required).
3. **Write passcode** to `0xDD01`, read it back, require a `Matched` reply.
4. **Get Config** for each zone (0..N) to learn available modes and limits.
5. **Get Status** (write `0xEE01`, read `0xFF01`) to read live state.
6. **Change** to set mode / setpoints / fan; expect an `RT:"OK"` acknowledgement.
7. **Poll** `Get Status` periodically to track changes.
8. **Disconnect** when done.

## Client Timing (as observed in the vendor app)

- The unit always has **4 config zones**: config is requested for zones 0–3
  one at a time (1 s retry until each reply arrives). Unused zones return a
  `CFG` containing only `Zone` (no `MAV`); a unit where all four zones report
  no `MAV` is unconfigured/faulted.
- `Get Status` is always sent with `"Zone":0` — the response's `Z_sts` carries
  every active zone, so there is no need to query zones individually.
- Status is polled every **4 s**; the connection is treated as lost after
  **3 consecutive** unanswered requests.
- Changes are **coalesced for ~1 s** before sending, so rapid adjustments
  become one `Change` message (a later value for the same key replaces the
  earlier one).
- After sending a `Change`, status responses are **ignored for ~3 s** — the
  unit can return pre-change state for a poll that was already in flight.
