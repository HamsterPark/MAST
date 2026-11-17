# The generic station

Some laboratories run MAST on a computer that has neither an STM (no Nanonis) nor a WITec
microscope: a spectrometer, a light source or two, a power meter, a temperature controller, a few
serial sensors. That is **a generic station**. It is the same service as on an STM or an optical
station, with the same jobs, tokens, e-stop, records and MCP tools. This page is the difference.
Most of what [the optical station page](09-optical-station.md) says about the operator and about
lasers holds here too.

## How you know which station you are on

Read `mast_briefing` first, as always. On a generic station its first section, `station`, says
`Station profile: generic`, lists the rules that apply and the configured devices.
`GET /api/health` (the operator's view) reports the same as `station_profile`, and what the station
has as `station_capabilities`. The profile is a setting (`station_profile`, or the environment
variable `MAST_STATION_PROFILE`) that the operator chooses; it is protected by the admin PIN and
takes full effect only after the MAST service is restarted. An agent never changes it.

## What is different

| | STM station | Optical station | Generic station |
|---|---|---|---|
| Instruments | the STM on its Nanonis controller | WITec, lasers, power meter, temperature controller | the devices in `config/optical_instruments.json` and the sensors in `config/environment_sensors.json` |
| Instrument skills | Nanonis skills, STM composites, STM analysis | optical bench, WITec, light sources, protocols, operator prompts | optical bench, light sources, protocols, operator prompts; no WITec skills |
| `mast_observe` | newest scan image and the Nanonis readbacks | newest spectrum and the optical devices | newest spectrum, the devices, the environment readings |
| Doing something | `mast_act` actions or `mast_run` | `mast_run` only | `mast_run` only: the named `mast_act` actions are refused with `not_available_on_this_station` |
| Briefing sections | tip, instrument, live, prefs, recent files, ... | station, optical state, operator prompts, recent spectra, ... | station, environment, optical state, operator prompts, recent spectra, ... |
| Serial auto-detection | on by default | always off | off unless the operator switches it on |

Everything else is unchanged: one act, one observation; acts run as jobs; `mast_wait`;
`mast_scope` before data-producing skills (a spectrum needs a sample); the literature and writing
tools; notes, GUIDE.md and WORKING.md; `mast_cancel` and `mast_emergency_stop`. On a generic station
the e-stop stops the light sources, shutters and stages; there is no Nanonis to stop.

A skill this station does not have the hardware for (a Nanonis skill, a WITec skill) is not
offered by `mast_find_skills`, and `mast_run` of one is refused with `skill_disabled`. That is the
station's decision, not a fault: do not look for a way around it.

## What needs a person

- **Steps that need hands.** Changing an optic, a sample holder or a cable, closing a vendor
  program: ask the operator with `mast_ask_operator`, then `mast_wait` for the reply. You can post
  a question and wait; you can never answer one.
- **The manual state.** What no software can read is signed by the operator. A generic station
  starts with one slot, `laser_area_safe`; the laboratory defines its own (which filter, which
  objective ...) in `config/optical_station.json`. Read them with `ReadOpticalState` or the
  `optical_state` briefing section; you never write them. Every spectrum's sidecar records the
  state that was signed when it was taken.
- **Laser emission.** Switching a light source on from software (`LaserEmissionOn`) needs two
  things: the device configuration allows it, and the operator has a current `laser_area_safe`
  attestation (valid for 8 hours). Switching emission off (`LaserEmissionOff`) is always allowed
  and is never refused.
- **Serial ports are exclusive.** A device whose vendor software is open cannot be opened by MAST.
  The skill fails and says which port; ask the operator to close that program.

## What `mast_observe` returns

`latest_frame` is the newest spectrum in the experiment folder, with a picture and the same numbers
as on an optical station (peaks, maximum counts, saturation); it is `null` while there is none.
Other files (sensor logs, images) are in the experiment folder: list them with `mast_list_data`.

`instrument` is the state echo:

| Field | Meaning |
|---|---|
| `lasers`, `temperature`, `power_meter`, `microscope` | the status of each device MAST **has already open** |
| `devices_closed` | devices that are configured but not open; observing never opens a device |
| `environment` | the newest reading of each configured sensor: `value`, `unit`, `status`, `age_s` |
| `optical_state` | the signed, expired and unset slots of the manual state |
| `operator_prompts_open` | how many questions are still waiting for the operator |

## Serial auto-detection

MAST can look for the gauges and thermometers it knows by probing every free serial port (it sends
an identity query and one Modbus frame to each). On a generic station that scan is off: the ports
may belong to a light source or to a vendor program. Sensors listed in
`config/environment_sensors.json` are opened as configured either way. The operator can switch the
scan on in the settings (`serial_autodetect`); it takes effect at the next start or rescan.

## A spectrum measurement

```text
mast_briefing                      read the station section and the rules
mast_scope                         choose the experiment and the sample
mast_observe                       newest spectrum, device state, environment, next_token
mast_find_skills "acquire spectrum"
mast_skill_card AcquireSpectrum
mast_run AcquireSpectrum after=<next_token>
mast_wait job                      the finished observation carries new_spectra and a picture
```

Read the numbers in `new_spectra` before the next step: check `saturated`, then the peaks. If the
state is not what the measurement needs, ask the operator instead of working around it.
