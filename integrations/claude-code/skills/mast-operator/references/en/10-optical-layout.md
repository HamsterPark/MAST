# The optical layout

The operator can draw the **optical layout**: a top-view drawing of the optical table. It shows where each mirror, lens, waveplate, laser and detector sits and which way it faces; what it is (model, manufacturer, function); whether it is motorized and where its motor is connected; and which MAST device or manual optical-state slot it stands for. MAST gives you the same picture, a ray trace through it and a plain-language description, so that you and the operator can talk about the real beam path instead of guessing at it.

Reading a layout opens no device and changes nothing. You can leave comments on it. You cannot change it, and you cannot resolve a comment (see [Why you cannot change it](#why-you-cannot-change-it)).

## Finding and reading a layout

- `mast_briefing` has a section `optical_layouts` once the operator has drawn something: how many layouts, how many components each, how many unresolved comments. With no layout the section is empty and takes no room in the briefing text.
- `mast_optical_layout` without `layout_id` lists the layouts. With `layout_id` it returns the description first (read it first, it is what the trace looks like in words), then a compact JSON with the geometry of every component. `image=true` attaches a PNG, zoomed to the components and the beams.
- Over HTTP the same is `GET /optical-layouts` and `GET /optical-layouts/{layout_id}` (with `layout`, `trace`, `summary`, `links_state` and `comments`) and `GET /optical-layouts/{layout_id}/render.png`; see the [API reference](07-api-reference.md).

The description comes in the language of the request (the `X-MAST-Lang` header, else the language MAST runs in) and stays within 8000 characters. Each kind of thing to look at shows at most 3 lines and one more line counting the rest, and no kind ever drops out; a description that would be longer first leaves out the spot sizes and foci, then shows one line of each kind, then cuts the beam paths (and says where). In English it reads like this:

```text
Optical layout "SHG excitation" (id shg, revision 12)
Table 1200 x 900 mm, hole pitch 25 mm (metric); 5 components, 1 light source; last saved by ana 2026-11-03 12:00 UTC
Coordinates: origin at the table's lower left, x to the right, y up, in mm; angles counter-clockwise from +x.

Beam paths (in the order along the beam):
1) L1 laser (532 nm, Cobolt Samba, power 50 mW, linear 90 deg, Ø0.7 mm beam at the exit, at (50, 62.5) mm, motorized: no)
   → M1 mirror (Thorlabs BB1-E02, throughput 99%, at (112.5, 62.5) mm, hit 0.0 mm off center, incidence 45.0 deg, path 62.5 mm) · arriving 50 mW · spot Ø0.70 mm · linear 90° (s)
   → HWP1 waveplate (half-wave, fast axis 45 deg, at (112.5, 200) mm, hit 0.0 mm off center, incidence 0.0 deg, path 200.0 mm, motorized: yes, K10CR1, connected to COM12, MAST device hwp_rot (open), slot excitation_waveplate currently = hwp_532) · arriving 49.5 mW · spot Ø0.73 mm · linear 90° (s)
   → F1 lens (focal length 50 mm, at (112.5, 250) mm, hit 0.0 mm off center, incidence 0.0 deg, path 250.0 mm) · arriving 49.5 mW · spot Ø0.74 mm · linear 0° (p)
     focus: 50.89 mm after F1, Ø46.63 µm
   → ends at D1 detector (at (112.5, 350) mm, hit 0.0 mm off center, incidence 0.0 deg, path 350.0 mm) · arriving 49.5 mW · spot Ø0.71 mm · linear 0° (p)

Things to look at: none found.
```

Each component along a beam ends with what arrives there: the power (`arriving 12.3 mW`, or `relative 24.6 %` of the source when the source has no `power_mw`), the spot (`spot Ø1.20 mm`, in µm below 0.1 mm; only for a source with `beam_diameter_mm`) and the polarization in words (`unpolarised`, `linear 30°`, with `(p)` or `(s)` near 0 or 90 degrees, `circular (χ = -45°)`, `elliptical ...`, `no light`). A segment with a beam waist in it adds a line such as `focus: 4.00 mm after OBJ, Ø0.81 µm`. A line `Assumptions:` comes first when one applies, and the things to look at also list a spot larger than a component's aperture, a beam that a component extinguishes, and waveplates without a retardance. In a layout with heights (see [Heights](#heights)) the description also gives the table's beam height, the height of every component that has one (a periscope's entrance and exit), the height of the beam at every hit, a line such as `↷ passes over M3 mirror (beam at 250 mm, its axis at 75 mm)` for a component a beam passes over or under (three per segment at most, then one line counting the rest), and three more kinds of things to look at: a hit off its axis in height by more than 20% of the aperture, a component nothing lights that a beam passes over or under, and a spot beyond the aperture in height.

## Conventions

Lengths are in millimetres, angles in degrees, wavelengths in nanometres. The origin is the lower left corner of the table, x points right, y points up (top view), and angles are counter-clockwise from +x, in [0, 360). Each component has a center, a direction `angle_deg` (where its local +x axis points) and an aperture: the *working surface* is the segment of that length through the center, perpendicular to the direction. What the direction means depends on the type:

| Kind of component | What `angle_deg` says, and what the trace does |
|---|---|
| Source (`laser`, `fiber_out`, `lamp`, `led`) | The direction of emission. One ray per wavelength from the center. |
| Mirror-like (`mirror`, `grating`, `sample` when it reflects, `custom` with `behavior` `reflect`) | The way the reflecting side faces. A ray arriving on that side reflects; one arriving from behind is stopped (`back_hit`). A grating is treated as a mirror (zero order). |
| Splitting (`beam_splitter`, `pbs`, `dichroic`) | The normal of the surface; both faces act. A splitter reflects the share `reflect_fraction` and transmits the rest; a polarizing beam splitter (`pbs`) transmits the p part and reflects the s part of whatever arrives; a dichroic reflects or transmits the whole beam by wavelength (`edge_nm`, `reflects`). |
| Transmitting (`lens`, `objective`, `waveplate`, `polarizer`, `window`, `nd`, `aotf`, `prism`, `periscope`, `custom` with `behavior` `transmit`) | The optical axis. The ray passes straight through, in either direction (a lens does not bend the ray; with `focal_mm` it focuses the Gaussian beam). |
| Conditional (`filter`, `iris`, `shutter`) | A filter passes a ray inside `pass_min_nm`..`pass_max_nm`, an iris stops a ray further than `open_mm / 2` from its center, a closed shutter stops everything. |
| Terminating (`detector`, `camera`, `spectrometer`, `power_meter`, `beam_dump`, `blocker`, `fiber_in`, `sample` when it does not reflect) | The face that receives light. A ray reaching it ends there; one arriving from behind is stopped (`back_hit`). A `fiber_in` whose `continues_at` names a `fiber_out` passes the light on: that output becomes a source of the same wavelength. |

## Power, polarization and beam size

The trace carries, along every ray, the power, the polarization and (when the source says how big its beam is) a Gaussian beam. It needs the props below; none of them is required, and a layout drawn without them traces as before.

| Prop | On | Meaning |
|---|---|---|
| `power_mw` | `laser`, `fiber_out` | Output power in mW. A source with several `wavelengths_nm` emits all of it at each wavelength. |
| `polarization` | `laser`, `fiber_out` | `linear` or `unpolarized`. Not given means unpolarized: then a polarizer or a `pbs` halves the power, and the description says so. |
| `pol_angle_deg` | `laser`, `fiber_out` | The direction of a `linear` polarization (only with `linear`; default 0). |
| `beam_diameter_mm` | `laser`, `fiber_out` | The 1/e² diameter at the exit, where the waist is. Without it there is no Gaussian beam. |
| `m2` | `laser`, `fiber_out` | Beam quality factor M² (only with `beam_diameter_mm`; default 1). |
| `throughput` | mirrors, gratings, lenses, objectives, waveplates, polarizers, filters, windows, AOTFs, prisms, periscopes, samples, `custom` | The share of the power that goes on: a mirror's reflectance, a part's transmittance, a polarizer's along its axis, a grating's zero order. Default 1. |
| `coupling` | `fiber_in` | The share of the arriving power that goes into the fiber (default 1). |

Polarization is described in each ray's own frame: `e_h` lies in the plane of the table, to the left of the beam, and `e_v` points up. Every angle is measured from `e_h` towards `e_v`, so 0 degrees is in the table plane (p for any reflection on the table) and 90 degrees is vertical (s). Because the frame turns with the beam, a mirror turns +45 degrees into -45 degrees (135): that is the physics of a reflection, not an error. The axis of a waveplate (`fast_axis_deg`) or a polarizer (`axis_deg`) is measured in the part's own frame: 0 degrees lies in the plane of the table, along the part's face (its local +y), and 90 degrees points straight up. Light travelling along the part's `angle_deg` (d·n > 0) comes in from the front and sees the axis as given; light from behind sees its mirror image (30 degrees reads as 150, that is -30). At oblique incidence the ray sees the axis of the physical part: a polarizer is modelled as an absorbing one (a wire grid or a dichroic sheet), which passes the field perpendicular to its absorbing axis and to the ray; a waveplate as a positive uniaxial crystal (quartz, MgF₂), whose fast wave is perpendicular to its optic axis and to the ray. A negative crystal (calcite) turns the other way at oblique incidence, and the change of retardance with the angle of incidence is not modelled. A waveplate needs a `retardance` (`half` or `quarter`) to act on the polarization; without one it is taken as transparent.

The power is the source's `power_mw` times every loss on the way: `throughput`, an ND filter's 10^-`od`, the projection onto a polarizer's axis, a splitter's share, the p or s part at a `pbs`. A fiber output continued from a `fiber_in` emits what reached that coupler (on the first branch that reached it) times its `coupling`, with its own `polarization`, `beam_diameter_mm` and `m2`. Light that is completely extinguished (crossed polarizers) is still traced, with power 0: the geometry does not depend on the power.

A Gaussian beam starts with its waist at the source and spreads with its Rayleigh range π w0² / (M² λ); a `lens` or `objective` with `focal_mm` changes it (thin lens, 1/q' = 1/q - 1/f) without bending the ray.

## Heights

The drawing is a top view, but components can sit at different heights and a periscope can lift or lower the beam. Three optional keys say so; a layout that gives none of them traces and reads exactly as before, everything at one height.

| Key | On | Meaning |
|---|---|---|
| `beam_height_mm` | `table` | The table's beam height in mm (default 75): every component and source without a `z_mm` of its own sits at it. |
| `z_mm` | `props` of any component | The height of its optical axis above the table top in mm (a source: of its exit), within ±10000; below 0 is below the table top. |
| `z_out_mm` | `props` of a `periscope` | The height of its exit (default: the height of its entrance). |

A ray always stays level; only a periscope changes its height. Where a ray meets a component in the plane but is more than half the aperture above or below the component's axis, it passes over or under it: nothing happens and the ray goes on. The working surface counts as a square, aperture by aperture. An iris with `open_mm` also stops a ray that is more than half its opening off the axis in height; a ray more than half the aperture off passes the iris altogether.

A periscope has a direction. Light that travels along its `angle_deg` (d·n > 0) enters at `z_mm` and leaves at `z_out_mm`; light the other way round enters at `z_out_mm` and leaves at `z_mm`. The beam keeps its offset (3 mm above the entrance comes out 3 mm above the exit, as two parallel 45-degree mirrors do it), and the vertical run inside the periscope is path: it is in `path_mm`, and the Gaussian beam spreads over it.

Not modelled: rays that leave the horizontal (other than inside a periscope), vertical beam paths other than a periscope's, mirrors tilted about a horizontal axis, refraction in three dimensions, a periscope that turns the beam (draw a periscope and a mirror), a periscope's body blocking light, and round working surfaces (the corners of the square count).

## What the trace says

The trace has one tree per light source and wavelength: `source`, `wavelength_nm` (`null` when unknown), `beam` and `branches`. A branch is a straight-segment path (`segments`) with the interactions along it (`events`), its `fraction` (the product of the splitting ratios on the way, which is not the power), and how it `end`s. Splitters make child branches `0.r` (reflected) and `0.t` (transmitted). Every event has `component`, `action`, the point `x`, `y`, and the numbers that matter for alignment:

- `offset_mm`: how far from the component's center the beam hits, measured along its working surface (signed; the local +y direction is positive). A beam hitting more than 20% of the aperture away from the center is listed as a problem.
- `incidence_deg`: the angle between the beam and the surface normal (0 = straight on).
- `path_mm`: the path length from the source up to this component. Use it for delay lines and pump-probe timing.
- `z_mm` and `dz_mm`: the height of the beam where it arrives, and how far that is from the component's axis (from the entrance of a periscope); above 0 the beam is above it. A segment has its height `z_mm` too, and `passes`: the components it passes over or under, each with `component`, the point `x`, `y` and `dz_mm` (above 0: over it). A pass is not an event and lights nothing; the whole trace records at most 5000 of them.

The beam is in a key `beam` on each tree (`source_mw`, the power of the source, and `m2`), each segment (the state where the segment starts: `p`, `s`, `q`) and each event (the state on arrival, before the component acts: `p`, `mw`, `d_mm`, `s`). `p` is the power relative to the source, with every loss; `mw` is the same in mW (`null` when the source has no `power_mw`); `s` is the normalised Stokes vector [S1, S2, S3] / S0 in the ray's frame (S1 horizontal minus vertical, S2 +45 minus -45; `null` when no light arrives); `q` is [z, zR] in mm, the Gaussian beam parameter (z > 0 past the waist); `d_mm` is the 1/e² diameter.

| Value | Where | Meaning |
|---|---|---|
| `reflect`, `transmit`, `split` | `action` | The beam was reflected, passed, or divided in two. |
| `stop`, `blocked`, `back_hit` | `action` and `end` | The beam ended at a terminating component; it was stopped by a filter, an iris or a closed shutter; it met the back of a component. |
| `left_table` | `end` | The beam left the table without meeting anything else. |
| `max_events` | `end` | The beam passed 64 components: probably a loop. |
| `budget` | `end` | The whole trace ran out of its 5000 events: this is the last branch and nothing after it was traced. |
| `split` | `end` | The branch ended at a splitter; its children continue. |

The whole trace records at most 5000 events, all light sources together. A layout that splits its light again and again (many beam splitters, many sources with many wavelengths) would otherwise make a trace of any size. When the limit is reached the trace stops: the last branch ends with `budget`, no later branch or beam is traced, and the description begins with "trace cut: ..." and leaves out what only a complete trace can tell (components no beam reaches, missed apertures, fibers without light). A real layout is far below the limit; if you see it, the drawing probably has beam splitters packed on top of each other, and the operator should look at it.

The trace is a top-view model with heights (see [Heights](#heights)): no refraction (a lens does not bend the ray), no aberrations, no dispersion, ideal mirrors (s and p reflect alike), no loss where an aperture clips the beam (it is only reported), no fiber mode matching; a grating only reflects in zero order. Where the description says a beam "passes just outside" a component, a ray crossed the line of that component's surface within one aperture of its center but outside the aperture, at the component's height: the component is probably meant to be hit and is not.

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
