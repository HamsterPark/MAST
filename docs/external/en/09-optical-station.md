# The optical station

Most of this guide describes MAST on an STM, where MAST drives a Nanonis controller. MAST also
runs on **an optical station**: a computer with a confocal Raman / SHG microscope (WITec) and its
lasers, power meter and temperature controller, and **no STM and no Nanonis at all**. It is the
same service, the same jobs, tokens, e-stop, records and MCP tools. What changes is what you see
and what needs a person. This page is the difference.

## How you know which station you are on

Read `mast_briefing` first, as always. On an optical station its first section, `station`, says
`Station profile: optical`, lists the rules that apply, and lists the configured optical devices.
`GET /api/health` (the operator's view) reports the same as `station_profile`. The profile is a
setting (`station_profile`, or the environment variable `MAST_STATION_PROFILE`) that the operator
chooses; it takes full effect only after the MAST service is restarted, because which background
threads exist is decided at start-up. Changing the setting needs the operator's admin PIN (it is one
of the PIN-protected settings): that is not yours to do, and an agent never changes the profile.

## What is different

| | STM station | Optical station |
|---|---|---|
| Instrument skills | Nanonis skills, STM composites, STM analysis | optical bench, WITec, lasers, power meter, temperature controller, operator prompts, and the generic literature / writing / experiment tools |
| `mast_observe` | newest scan image and the Nanonis readbacks (bias, current, Z) | newest spectrum (a picture and its numbers) and the state of the optical devices |
| Doing something | `mast_act` actions or `mast_run` | `mast_run` only: the named `mast_act` actions (scan, withdraw ...) are refused with `not_available_on_this_station` |
| Briefing sections | tip, instrument, live, prefs, recent files, ... | station, optical state, operator prompts, recent spectra, ... (no tip, instrument, live, prefs or recent files) |
| Nanonis | connected, polled, watched | not used: nothing is dialled and no Nanonis thread runs |

Everything else is unchanged: one act, one observation; acts run as jobs; `mast_wait`;
`mast_scope` before data-producing skills (a spectrum needs a sample); notes, GUIDE.md and
WORKING.md; `mast_cancel` and `mast_emergency_stop`.

An STM-only skill is not offered by `mast_find_skills`, and `mast_run` of one is refused with
`skill_disabled`. That is the station's decision, not a fault: do not look for a way around it.

## What needs a person

An optical station has steps no software can do, and states no software can read. MAST makes the
operator the only one who can attest to them.

- **WITec write access.** WITec Control only lets a remote client change anything after the operator
  has pressed *COM Automation*; that locks the GUI for the person until they press *Revoke*. Without
  it, skills that write fail plainly. Do not retry in a loop: ask the operator with
  `mast_ask_operator`, then `mast_wait` for the reply.
- **Manual optics.** Which λ/2 plate is in the excitation path, the analyzer optic, the manual
  polarizer angle, the objective, the edge filter and the pump-current percentage of the 1064 nm
  laser are set by hand. The operator signs them; you can read them (`ReadOpticalState`, or the
  `optical_state` briefing section) but never write them. Every spectrum's sidecar records the
  state that was signed when it was taken. When the experiment needs a different plate or objective,
  ask the operator to change it and to sign the new state.
- **Laser emission.** Switching a laser on from software needs two things: the device
  configuration allows it, and the operator has a current *laser area safe* attestation (valid
  for 8 hours). Switching emission off is always allowed and is never refused.
- **Serial ports are exclusive.** A laser or controller whose vendor software is open cannot be
  opened by MAST. The skill fails and says which port; ask the operator to close that program.
  MAST does not scan serial ports on an optical station.

You can post a question and wait; you can never answer one. `mast_wait` with the `operator_reply`
condition waits for the answer (set your MCP tool timeout to at least 900 s).

## What `mast_observe` returns

`latest_frame` is the newest spectrum in the experiment folder, with a picture and these numbers:

| Field | Meaning |
|---|---|
| `peaks` | the most prominent peaks, strongest first: `nm`, `raman_shift_cm1` (when the laser line is known), `counts`, `prominence` |
| `max_counts`, `max_at` | maximum of the mean spectrum and where |
| `frame_max_counts`, `saturated`, `saturation_threshold` | the largest value in any frame; saturated when it reaches the threshold (60000 counts by default); a clipped spectrum can look fine |
| `exposure_ms`, `frames`, `laser_nm` | how it was taken |
| `file`, `path`, `age_s` | where it is and how old it is |

`instrument` is the state echo: `microscope`, `lasers`, `temperature` and `power_meter` hold the status of
each device MAST **has already open**; devices that are configured but not open are listed under
`devices_closed`. Observing never opens a device: opening is what a skill does, on purpose,
so that an observation can never take a port away from the vendor software. `optical_state` lists
the signed, expired and unset slots, and `operator_prompts_open` counts the questions still waiting.

## A spectrum measurement

```text
mast_briefing                      read the station section and the rules
mast_scope                         choose the experiment and the sample
mast_observe                       newest spectrum, device state, next_token
mast_find_skills "acquire spectrum"
mast_skill_card AcquireSpectrum
mast_run AcquireSpectrum after=<next_token>
mast_wait job                      the finished observation carries new_spectra and a picture
```

Read the numbers in `new_spectra` before the next step: check `saturated`, then the peaks. If the
state is not what the measurement needs, post a question for the operator instead of working
around it.
