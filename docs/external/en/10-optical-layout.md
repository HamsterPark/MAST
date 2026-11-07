# The optical layout

The operator can draw the **optical layout**: a top-view drawing of the optical table. It shows where each mirror, lens, waveplate, laser and detector sits and which way it faces; what it is (model, manufacturer, function); whether it is motorized and where its motor is connected; and which MAST device or manual optical-state slot it stands for. MAST gives you the same picture, a ray trace through it and a plain-language description, so that you and the operator can talk about the real beam path instead of guessing at it.

Reading a layout opens no device and changes nothing. You can leave comments on it. You cannot change it, and you cannot resolve a comment (see [Why you cannot change it](#why-you-cannot-change-it)).

## Finding and reading a layout

- `mast_briefing` has a section `optical_layouts` once the operator has drawn something: how many layouts, how many components each, how many unresolved comments. With no layout the section is empty and takes no room in the briefing text.
- `mast_optical_layout` without `layout_id` lists the layouts. With `layout_id` it returns the description first (read it first, it is what the trace looks like in words), then a compact JSON with the geometry of every component. `image=true` attaches a PNG, zoomed to the components and the beams.
- Over HTTP the same is `GET /optical-layouts` and `GET /optical-layouts/{layout_id}` (with `layout`, `trace`, `summary`, `links_state` and `comments`) and `GET /optical-layouts/{layout_id}/render.png`; see the [API reference](07-api-reference.md).

The description comes in the language of the request (the `X-MAST-Lang` header, else the language MAST runs in) and is cut at 8000 characters. In English it reads like this:

```text
Optical layout "SHG excitation" (id shg, revision 12)
Table 1200 x 900 mm, hole pitch 25 mm (metric); 4 components, 1 light source; last saved by ana 2026-11-03 12:00 UTC
Coordinates: origin at the table's lower left, x to the right, y up, in mm; angles counter-clockwise from +x.

Beam paths (in the order along the beam):
1) L1 laser (532 nm, Cobolt Samba, at (50, 62.5) mm, motorized: no)
   → M1 mirror (Thorlabs BB1-E02, at (112.5, 62.5) mm, hit 0.0 mm off center, incidence 45.0 deg, path 62.5 mm)
   → HWP1 waveplate (half-wave, at (112.5, 200) mm, hit 0.0 mm off center, incidence 0.0 deg, path 200.0 mm, motorized: yes, K10CR1, connected to COM12, MAST device hwp_rot (open), slot excitation_waveplate currently = hwp_532)
   → ends at D1 detector (at (112.5, 300) mm, hit 0.0 mm off center, incidence 0.0 deg, path 300.0 mm)

Things to look at: none found.
```

## Conventions

Lengths are in millimetres, angles in degrees, wavelengths in nanometres. The origin is the lower left corner of the table, x points right, y points up (top view), and angles are counter-clockwise from +x, in [0, 360). Each component has a center, a direction `angle_deg` (where its local +x axis points) and an aperture: the *working surface* is the segment of that length through the center, perpendicular to the direction. What the direction means depends on the type:

| Kind of component | What `angle_deg` says, and what the trace does |
|---|---|
| Source (`laser`, `fiber_out`, `lamp`, `led`) | The direction of emission. One ray per wavelength from the center. |
| Mirror-like (`mirror`, `grating`, `sample` when it reflects, `custom` with `behavior` `reflect`) | The way the reflecting side faces. A ray arriving on that side reflects; one arriving from behind is stopped (`back_hit`). A grating is treated as a mirror (zero order). |
| Splitting (`beam_splitter`, `dichroic`) | The normal of the surface; both faces act. A splitter reflects the share `reflect_fraction` and transmits the rest; a dichroic reflects or transmits the whole beam by wavelength (`edge_nm`, `reflects`). |
| Transmitting (`lens`, `objective`, `waveplate`, `polarizer`, `window`, `nd`, `aotf`, `prism`, `periscope`, `custom` with `behavior` `transmit`) | The optical axis. The ray passes straight through, in either direction. |
| Conditional (`filter`, `iris`, `shutter`) | A filter passes a ray inside `pass_min_nm`..`pass_max_nm`, an iris stops a ray further than `open_mm / 2` from its center, a closed shutter stops everything. |
| Terminating (`detector`, `camera`, `spectrometer`, `power_meter`, `beam_dump`, `blocker`, `fiber_in`, `sample` when it does not reflect) | The face that receives light. A ray reaching it ends there; one arriving from behind is stopped (`back_hit`). A `fiber_in` whose `continues_at` names a `fiber_out` passes the light on: that output becomes a source of the same wavelength. |

## What the trace says

The trace has one tree per light source and wavelength: `source`, `wavelength_nm` (`null` when unknown) and `branches`. A branch is a straight-segment path (`segments`) with the interactions along it (`events`), its `fraction` of the source's intensity, and how it `end`s. Splitters make child branches `0.r` (reflected) and `0.t` (transmitted). Every event has `component`, `action`, the point `x`, `y`, and three numbers that matter for alignment:

- `offset_mm`: how far from the component's center the beam hits, measured along its working surface (signed; the local +y direction is positive). A beam hitting more than 20% of the aperture away from the center is listed as a problem.
- `incidence_deg`: the angle between the beam and the surface normal (0 = straight on).
- `path_mm`: the path length from the source up to this component. Use it for delay lines and pump-probe timing.

| Value | Where | Meaning |
|---|---|---|
| `reflect`, `transmit`, `split` | `action` | The beam was reflected, passed, or divided in two. |
| `stop`, `blocked`, `back_hit` | `action` and `end` | The beam ended at a terminating component; it was stopped by a filter, an iris or a closed shutter; it met the back of a component. |
| `left_table` | `end` | The beam left the table without meeting anything else. |
| `max_events` | `end` | The beam passed 64 components: probably a loop. |
| `budget` | `end` | The whole trace ran out of its 5000 events: this is the last branch and nothing after it was traced. |
| `split` | `end` | The branch ended at a splitter; its children continue. |

The whole trace records at most 5000 events, all light sources together. A layout that splits its light again and again (many beam splitters, many sources with many wavelengths) would otherwise make a trace of any size. When the limit is reached the trace stops: the last branch ends with `budget`, no later branch or beam is traced, and the description begins with "trace cut: ..." and leaves out what only a complete trace can tell (components no beam reaches, missed apertures, fibers without light). A real layout is far below the limit; if you see it, the drawing probably has beam splitters packed on top of each other, and the operator should look at it.

The trace is a top-view geometric model: no height, no focusing by lenses, no refraction, polarization or dispersion; a grating only reflects in zero order. Where the description says a beam "passes just outside" a component, a ray crossed the line of that component's surface within one aperture of its center but outside the aperture: the component is probably meant to be hit and is not.

## What a link says

A component can name a MAST device (`links.device_id`) and a manual optical-state slot (`links.slot`). The links are only references; the description (and `links_state` over HTTP) adds what they say now: whether the device is enabled and open, its status if the process already has it open, and the slot's current signed value. Reading never opens a device.

## Commenting

`mast_optical_layout_comment` adds a comment to a layout, optionally about one component (`component_id`, which must exist in the layout). Use it to ask what a component is, to point out what you see in the description (a beam that misses a mirror, a model number missing, a path length you need), or to suggest a change. The author is recorded as your agent id; the operator sees the comment in the layout page, marked as coming from an agent, and resolves it.

You may have 50 unresolved comments on a layout, and a layout holds 1000 comments; a comment is at most 2000 characters.

| Error | HTTP | Meaning |
|---|---|---|
| `unknown_layout` | 404 | No layout with that id. |
| `layout_unreadable` | 500 | The layout's file cannot be read; it is left untouched. Tell the operator. |
| `invalid_comment` | 422 | The text is empty or too long, or `component_id` is not in the layout. |
| `too_many_open_comments` | 429 | You already have 50 unresolved comments here: wait for the operator. |
| `layout_comments_full` | 429 | The layout holds 1000 comments, none of them resolved. |

## Why you cannot change it

The layout records what is physically on the table, and only the operator knows that: which mirror was swapped this morning, where a fiber really goes. If an agent could move a mirror in the drawing or rewrite a model number, every question the drawing answers would become a guess. So the layout can only be edited in the operator's page. This is the same rule that keeps you from answering an operator prompt or signing the optical state (see [The optical station](09-optical-station.md)): when something in the layout looks wrong, say so in a comment; when the work cannot wait, ask with `mast_ask_operator` and `mast_wait` for the reply.

| Tool | What it does |
|---|---|
| `mast_optical_layout` | List the layouts, or read one (description, geometry, optionally a picture). |
| `mast_optical_layout_comment` | Add a comment to a layout. |
