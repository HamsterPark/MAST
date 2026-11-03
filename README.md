# MAST — Modular Autonomous SPM Toolkit

**English** · [中文](README.zh.md)

**Hardware support:** MAST currently supports only Nanonis controllers (a programming interface license is required) and selected optical components in the software's TERS section. We plan to expand support to other STM controllers and more optical components.

**Getting started**

**On the instrument computer**

1. Download the [MAST Windows installer](https://github.com/HamsterPark/MAST/releases), install it on the computer connected to your instrument, and open the MAST launcher.
2. Start the main service in the launcher.
3. Install Claude Code or Codex on the same computer.
4. Open Claude Code or Codex and tell it: “Use MAST on this computer to help me with the following experiment: …”

**From another computer**

1. Install MAST on the instrument computer and open the MAST launcher.
2. In the launcher, check the box to enable remote connections, set a username and password, and then start the main service.
3. Install Claude Code or Codex on the computer you want to work from.
4. Install Tailscale on both computers and sign in to the same Tailscale account.
5. Follow the guide in the MAST launcher to install the OpenSSH service on the instrument computer. This allows the other computer to connect to it.
6. Copy the connection details and key provided by the guide into Claude Code or Codex on your other computer. Tell it: “Connect to the instrument computer and use MAST to help me with the following experiment: …”

You can view progress, operate the instrument manually, or stop a task in the MAST interface.

This repository contains the **MAST 6.5.0 public source snapshot**; Windows installers have separate version numbers. See [release scope and validation](#validation-and-public-release-scope) for source availability and validation records.

**Connecting general-purpose AI agents to atomic-scale experiments.**

MAST supports **scanning tunneling microscopy (STM) and tip-enhanced Raman spectroscopy (TERS)** with instrument interfaces and constrained execution. STM control currently uses Nanonis controllers; the software's TERS section supports selected optical components. An external agent harness manages reasoning, context, and task organization; MAST supplies instrument skills, parameter checks, observation access, action records, and operator intervention. The integrated VISTA-SPM layer organizes visual evidence and quantitative measurements for the agent.

The scientific aim is to sustain a cycle of **observation, hypothesis, measurement, and revision** as tip, sample, and measurement conditions evolve. Our development effort now centers on the experimental interfaces and evidence needed to support that cycle.

![Python 3.13](https://img.shields.io/badge/python-3.13-blue) ![TypeScript](https://img.shields.io/badge/frontend-React%2018%20%2B%20TypeScript-blue) ![MIT license](https://img.shields.io/badge/license-MIT-green) ![Source snapshot date](https://img.shields.io/badge/snapshot-2026-09-21-lightgrey)

[Design direction](#why-external-agents-and-vista-spm) · [Experiments](#real-instrument-demonstrations) · [Scientific background](#scientific-background-and-experimental-challenges) · [Architecture](#capabilities-and-architecture) · [Validation](#validation-and-public-release-scope) · [Documentation](#documentation-and-contributions) · [Related projects](#related-projects-and-experimental-reasoning)

## Why external agents and VISTA-SPM

We previously developed an in-house multi-agent orchestrator, custom experimental workflows, and specialized STM vision models based on DINOv3. **We have stopped developing the built-in agent workflows and the dedicated DINOv3 training approach.** Our recommended direction is now external agent harnesses for reasoning and task organization, MAST for instrument execution, and VISTA-SPM for visual reasoning support. The 6.5.0 source snapshot retains earlier implementations as a record of that work.

### Visual reasoning through a harness

[VISTA](https://arxiv.org/html/2610.02200v1) demonstrates that general-purpose multimodal models can perform strong visual reasoning in the interactive environments it evaluates when given direct image access, lossless visual memory, and tools for retrieving and inspecting observations. These results motivate our choice to adapt that approach to STM through VISTA-SPM.

Training a specialized vision model requires data collection, annotation, compute, tuning, and repeated validation. For our project, this demands substantial time and funding. New samples, tip conditions, imaging contrast, and experimental tasks continually introduce new generalization requirements. Improvements in general-purpose multimodal LLMs make it more useful to invest in access to original evidence, calibrated coordinates, and quantitative measurement tools. VISTA-SPM provides that scientific scaffolding, while the general-purpose model interprets the observations. Generalization is evaluated as experiments and samples change.

### Reusing the progress of external agent harnesses

Agent harnesses provide the environment for tool use, context management, task organization, and recovery. Their rapid development makes maintaining a separate in-house workflow stack increasingly difficult to justify: it duplicates general capabilities and is hard to keep aligned with improvements across the broader ecosystem. We have therefore moved planning and orchestration to external agents and made their interface the focus of MAST development.

**Our priority is the experimental layer:** instrument interfaces, execution constraints, visual evidence, physical measurements, traceable records, and experimental evaluation. The goal is to bring improvements in general models and agent harnesses into laboratory work with minimal instrument-specific adaptation. This also directs our time and resources toward the scientific problems that require experimental expertise.

## Real-instrument demonstrations

Two independent demonstrations illustrate tip conditioning and sustained STM operation. The records and selected figures are supplied by the maintainer; complete experimental datasets and logs are outside this source release. The long campaign used **MAST 6.4.0**. Validation of the newer external interface is listed separately below.

### Tip conditioning on Au and atomic-resolution imaging

![Eight STM frames and a relative timeline of tip conditioning on Au through MAST](docs/assets/au-tip-repair-overview.png)

*An external agent used an earlier MAST control path to condition the tip on an Au surface and obtain atomically resolved STM images. This task was separate from the long campaign.*

### A nearly 100-hour STM campaign

The agent repeatedly relocated a drifting target, acquired spectra and images, and resumed acquisition after communication faults. The researcher supplied scientific goals and time budgets and paused the experiment for liquid-nitrogen refills; the agent scheduled the main measurements through MAST without individual instrument commands from the researcher.

The main event log spans **2026-09-17 23:10:37 to 2026-09-22 02:08:10 (UTC+8)**, or **98 hours 57 minutes**. This is the span of the record, including waits and interruptions. Nominal conditions were approximately **4.3 K (−269 °C)**, with a common reference setpoint of **20 pA** and a subnanometer tunneling gap. The early tracking segment followed a feature with an apparent-height variation of approximately **18 pm**; measurement setpoints varied during the campaign.

![Spiral clock of timestamped operations in the completed campaign](docs/assets/long-run-clock.svg)

*Each turn represents 24 hours. Gold ticks denote spectra, orange arcs imaging, dark diamonds local tip-manipulation attempts, and open circles resumed acquisition after communication faults.*

| Measurement or activity | Final main-log total |
|---|---|
| Record span | **98 hours 57 minutes**, September 17–22 |
| Spectroscopy | **677** spectra and **272,727** sampled points; **24.4 hours** of acquisition |
| Imaging | **267** completed frames; **25.3 hours** of scanning |
| Tip movement and waiting | **485** moves; **1,671** waits with requested durations totaling **21.9 hours** |
| Target tracking | **116** successful re-registrations; median position-prediction error approximately **25 pm** |
| Experimental record | **5,805** timestamped events covering measurements, waits, checks, and exceptions |

Event counts include activity beyond instrument commands. Wait durations are the logged requested values. These totals cover the main log; a separate earlier preparation series is excluded.

![Time-lapse of early tracking frames and the drift trajectory](docs/assets/long-run-drift.gif)

*The animation pairs 46 early tracking images with the target's position trajectory. The table covers the full main-log window.*

<details>
<summary>Scheduling, recovery, and revision of interpretations</summary>

| Experimental function | Evidence from this campaign |
|---|---|
| Overnight acquisition | Between 22:00 and 06:00, the agent acquired **202** spectra and **126** images and made **132** tip moves. |
| Time and spatial constraints | A measurement that would overrun the handover was removed from the plan; a plan extending beyond the confirmed scan area was rejected. |
| State verification and recovery | Two resumptions after communication faults are marked on the clock. A separate feedback-state mismatch was detected and corrected before acquisition continued. |
| Traceability and interpretation | Preliminary interpretations contradicted by subsequent review were revised. Raw images and spectra, failed attempts, timestamped actions, and the revision history remain in the experimental record. |

</details>

<details>
<summary>Selected measurements: spectroscopy, spatial sampling, and imaging</summary>

The original measurements are arranged into three multi-panel figures. Axes, units, and essential settings are retained; sample names and internal site labels have been removed.

![Reference spectra, a time-ordered heat map, and tip-height response](docs/assets/long-run-results-spectra.png)

![Local spectra, two current grids, and line spectroscopy](docs/assets/long-run-results-spatial.png)

![STM images, lock-in X-channel maps, and an apparent-height timeline](docs/assets/long-run-results-imaging.png)

*The two grids in the second figure map current at −0.5 V under different measurement settings. Panels d–e of the third figure show the lock-in X channel; panel f extends to approximately hour 76. These selected figures cover portions of the campaign.*

</details>

<a id="what-is-stm"></a>

## Scientific background and experimental challenges

STM measures quantum tunneling between a conductive tip and a sample. During typical constant-current imaging, feedback adjusts the tip height to maintain a current setpoint, producing atomic-scale images whose contrast depends on both surface geometry and electronic structure. Scanning tunneling spectroscopy (STS) measures current or differential conductance as a function of bias. Relating differential conductance to the sample's local density of states requires assumptions about the tip, tunneling barrier, temperature, and measurement conditions. See [Binnig et al.](https://doi.org/10.1103/PhysRevLett.49.57) and [Tersoff–Hamann](https://doi.org/10.1103/PhysRevB.31.805).

Autonomous STM must establish credible measurement conditions while investigating the sample. The atomic configuration, chemistry, and electronic states of the tip apex are generally not observed directly. Images and spectra constrain possible tip–sample states, often without identifying a unique explanation. Scanning, pulses, contact, and material transfer can change those states during the experiment.

| Physical property | Consequence for experimental decisions |
|---|---|
| **Partial observability** | Maintain testable hypotheses about tip and sample conditions; use repeat scans, reference measurements, and controls to distinguish explanations. |
| **Strong nonlinearity** | Tunneling current is highly sensitive to distance. Select actions using current calibration and operating conditions, with explicit parameter bounds. |
| **Nonstationarity** | Thermal drift, piezoelectric creep, adsorption, and tip changes alter position or response over time. Recheck registration and measurement conditions throughout the experiment. |
| **History dependence** | Piezoelectric hysteresis, previous pulses, contacts, and material transfer affect subsequent behavior. Retain the sequence, timing, and outcome of earlier operations. |
| **Actions modify the system** | Tip conditioning and atom manipulation deliberately change the junction or surface; measurement can also perturb a sample. Evaluate physical consequences and recheck the state after an action. |

A well-resolved image alone does not establish that the tip is suitable for spectroscopy. An anomalous spectrum may require tests of tip states, charging, [tip-induced band bending](https://doi.org/10.1103/PhysRevLett.70.2471), feedback, or noise. Relevant controls include changing measurement conditions, revisiting a reference area, or repeating a measurement after tip conditioning. Instrument calibration, sample conditions, and operator authorization determine which controls are admissible.

Decisions about when to wait, condition the tip, or repeat a reference measurement depend on experimental expertise. Bringing that expertise into autonomous workflows requires general STM knowledge to be combined with instrument-specific calibration, operating procedures, and current observations.

MAST's research objective is to support the full experimental cycle:

**Observe → formulate hypotheses → design controls → execute within constraints → check changes to tip and sample → incorporate new evidence and revise interpretations → select the next measurement.**

This objective connects STM expertise with decision-making under partial observability, online system identification, and active learning. It describes the research direction; the demonstrations and validation table define the capabilities evidenced by this release.

Published work on [reinforcement learning for atom manipulation](https://www.nature.com/articles/s41467-022-35149-w) and [integrated STM automation](https://doi.org/10.1021/acs.nanolett.5c04982) provides relevant experimental context. Reviews of [self-driving SPM](https://doi.org/10.1021/acs.accounts.6c00273) and [AI-assisted SPM](https://doi.org/10.1016/j.asi.2026.100003) discuss the broader progression from individual automated tasks to closed-loop experiments.

<details>
<summary>Instrument conditions and measurement background</summary>

For low-energy spectroscopy, low-temperature UHV STM combines a sensitive tunneling junction with demanding environmental control. Typical scales depend on the sample, task, and instrument.

| Condition | Experimental significance |
|---|---|
| Tip–sample separation of a few ångströms | The approximate dependence $I \propto e^{-2\kappa z}$ gives high distance sensitivity. Here, $z$ is the separation and $\kappa$ depends on the effective barrier; tip and sample electronic states also affect the measured response. |
| Picoampere to nanoampere currents | Signals commonly range from approximately $10^{-12}$ to $10^{-9}$ A. Preamplifiers, grounding, cabling, and bandwidth determine the measurement noise. |
| Kelvin or millikelvin temperatures | Cooling reduces thermal broadening; thermal equilibration and electron temperature remain important for spectroscopy. |
| Ultrahigh vacuum | Reduced residual-gas adsorption supports clean surfaces and long measurements, while tip changes remain possible. |
| Mechanical and electrical isolation | Vibration isolation, shielding, and filtering suppress junction motion and electrical disturbances. |
| Stability over hours or longer | Repeated spectra and maps require spatial registration and comparable tip conditions despite drift and creep. |

For current scales, see the [single-molecule measurement review](https://doi.org/10.1021/nn103298x). Published examples include a [10 mK scanning probe facility](https://www.nist.gov/publications/10-mk-scanning-probe-microscopy-facility) and a [system designed for microelectronvolt tunneling resolution](https://doi.org/10.1063/5.0005320). These are properties of the cited instruments. STM also operates at room temperature, in air, and in liquids; low temperature and UHV are specific experimental choices. [Yothers et al.](https://doi.org/10.1063/1.4974271) discuss correction of drift and scanner nonlinearities.

</details>

## Capabilities and architecture

The current division of responsibilities places reasoning and task organization in an external agent harness. MAST provides the instrument backend and execution checks, VISTA-SPM provides visual memory and measurement tools, and the Nanonis controller handles low-level feedback and instrument control.

**Researcher → external agent / harness → MAST tools and execution checks → Nanonis / STM**

**Instrument data → VISTA-SPM: archive, inspect, and measure → external-agent reasoning**

The table below lists current usage entry points alongside source references from the 6.5.0 snapshot. VISTA-SPM is integrated into current MAST; its public reference implementation and validation records are available in the companion repository.

| Experimental requirement | Mechanism | Implementation or guide |
|---|---|---|
| Support TERS experiments | Control supported optical components through the TERS interface | TERS section in the MAST application |
| Connect an external agent | Skill discovery, asynchronous jobs, request deduplication, conflict checks, and restart-state handling | [External jobs](MASTv2/mast/api/ext/jobs.py), [Claude Code integration](integrations/claude-code/README.md) |
| Execute bounded physical operations | Parameter and SI-magnitude validation, execution-mode checks, instrument ownership, and abort handling | [Execution context](MASTv2/mast/core/execution_context.py), [safety checks](MASTv2/mast/core/safety.py) |
| Relate reasoning to recent observations | Buffering and monitoring expose observation age and scan progress | [Buffer service](MASTv2/mast/buffer/service.py), [snapshot observation tools](MASTv2/mast/agents/_shared/buffer_tools.py) |
| Inspect and measure visual evidence | Persistent images, calibrated coordinates, regional inspection, profiles, registration, and quantitative readout | [VISTA-SPM](https://github.com/HamsterPark/VISTA-SPM) |
| Detect and handle communication failures | Controller-protocol reads, timeouts, reconnection and polling fallback, and explicit stream-completion states | [Controller protocol](MASTv2/mast/core/nanonis_patch.py), [interface communication](frontend/src/lib/) |

The React operator interface supports monitoring, manual operations, intervention, and abort. Manual and external execution paths reuse execution checks; instrument ownership is process-local. The [review guide](AGENTS.md) maps the snapshot's implementation to regression tests.

<details>
<summary>Historical work: built-in agents and DINOv3-based vision</summary>

Earlier MAST versions implemented an orchestrator, seven domain agents, typed artifact handoffs, and custom planning/runtime paths. We also developed specialized STM vision models using DINOv3 backbones, adaptation, and task-specific heads. These efforts informed our understanding of instrument state, observation interfaces, task handoffs, and the cost of maintaining sample-specific perception.

Their implementations remain in the public snapshot for source review. They are no longer the project's recommended development or usage path. Historical entry points include [agent topology](docs/v2/agent-topology.md), [state contracts](MASTv2/mast/agents/state.py), [artifact handoffs](MASTv2/mast/agents/_shared/artifact_channel.py), [the vision module](MASTv2/mast/vision/module.py), and [the DINOv3-based backend](MASTv2/mast/vision/vigil_backend.py). Model weights and training data are outside the public snapshot.

</details>

Further reading: [instrument skill catalog](docs/v2/skill-catalog.md) and [model-provider adapters in the source snapshot](docs/api_providers/).

## Validation and public release scope

The records below describe specific versions and execution paths. They include historical implementations; the current project direction is the external-agent and VISTA-SPM approach above:

| Component or record | Version and evidence | Scope |
|---|---|---|
| Long STM campaign | MAST **6.4.0**; timestamped record spanning approximately 99 hours | Maintainer-reported real-instrument operation through the earlier external control path; selected figures are public. |
| Public source snapshot | MAST **6.5.0**; software baseline `9884ff5`, tested **2026-09-21** | Backend, frontend, and release checks using test doubles and synthetic data. |
| External API and MCP integration | **6.5.0** snapshot of `/api/ext/v1` | The snapshot records software tests; it does not include hardware validation for this path. |
| Built-in orchestration and DINOv3-based vision | Earlier research and development retained in the source snapshot | Historical implementations; further development of these approaches has stopped. Current work uses external agents and VISTA-SPM. |
| Other snapshot extensions | The skill workshop and marketplace, and qPlus paths | Hardware validation was pending in the recorded snapshot. |

The maintainer also reports functional instrument use of core execution, skills, internal agents and APIs, vision, monitoring, data I/O, buffering, updates, and networking in the versions deployed at the time.

<details>
<summary>Recorded software validation: environment, counts, and scope</summary>

The recorded platform was **Windows / Python 3.13**, with source baseline **`9884ff5`**.

| Check | Recorded result |
|---|---|
| Backend tests | **14,356 passed, 0 failed**; 46 skipped and 2 expected failures |
| Frontend unit tests | **985 / 985 passed** |
| Frontend build and type checking | Passed |
| API contract | 302 OpenAPI paths and 577 schema definitions; frontend types synchronized |
| Release checks | Python syntax, import closure, and public-content cleanup passed |

These are historical software results for that baseline. Documentation edits do not rerun or extend this validation. The [validation record](docs/OPEN_SOURCE_NOTES.md#四import-闭包与测试) documents commands, optional dependencies, skips, and reproduction requirements. Tests use instrument doubles; a complete instrument simulator is not bundled.

</details>

The public source preserves the 6.5.0 implementation: instrument skills, execution checks, external-agent integration, data processing, the React interface, contribution examples, and tests, alongside historical built-in orchestration and vision code. Gallery and literature-management implementations are included with empty initial data. The presence of the earlier code records the project history; it does not make those approaches the current recommendation.

Selected specialized modules, literature-derived skills, knowledge assets, vision weights, instrument calibration, site configuration, full experimental datasets, and private development history are excluded for licensing, intellectual-property, commercialization, and research-data reasons. Deployment requires the corresponding configuration and assets; the built-in preprocessing profile is an uncalibrated example. The [public release notes](docs/OPEN_SOURCE_NOTES.md) describe these omissions and their effects.

<details>
<summary>Source snapshot inventory</summary>

| Metric | Count |
|---|---|
| Python files / physical lines in `MASTv2/mast/` | 843 / 315,846 |
| Handwritten TypeScript lines in `frontend/src/` | 65,628; generated `schema.d.ts` excluded |
| Skill classes defining `execute()` | 444; built-in and composite skills |
| HTTP / WebSocket route declarations | 365 |
| Test function definitions in `tests/` | 11,659; parameterized cases not expanded |

These are static counts of the exported tree. Runtime registrations, OpenAPI paths, and expanded test cases use different counting methods. Development began on 2026-03-17; the development repository had 1016 commits at export.

The historical agent topology comprises one orchestrator; seven domain agents for research coordination, literature, experimental design, instrument control, data processing, paper writing, and paper review; and two auxiliary nodes for brainstorming and buffer summaries. The snapshot also retains `conduct/`, `agentruntime/`, and `goals/`. Its custom runtime coexists with LangGraph, with `engine_v2_*` switches off by default; these details describe the archived design.

</details>

## Documentation and contributions

| Task | Entry point |
|---|---|
| External-agent interface reference | [Operator guide](docs/external/en/README.md), [quickstart](docs/external/en/01-quickstart.md), and [Claude Code plugin](integrations/claude-code/README.md) |
| Inspect the source snapshot and run local checks | [Repository guide](AGENTS.md): source routes, Python 3.13 / Node 24 setup, and tests without instrument access; built-in orchestration is historical |
| Contribute an instrument skill | [Contribution guide](CONTRIBUTING.md): submissions under `contrib/skills/`, with maintainer hardware validation before inclusion in the official skill tree |
| Inspect dependencies and public-release boundaries | [Release notes](docs/OPEN_SOURCE_NOTES.md) and [third-party notices](THIRD_PARTY.md); optional PDF extraction and OCR dependencies are in `MASTv2/requirements-pdf.txt` |

## Related projects and experimental reasoning

**Representation is part of reasoning.** An STM image becomes more useful for experimental decisions when its coordinates, measurement conditions, and action history remain available. Revisiting earlier observations supports comparison of explanations, spatial registration, and selection of informative controls.

[VISTA-SPM](https://github.com/HamsterPark/VISTA-SPM) is integrated into current MAST as its visual reasoning layer. It adapts [VISTA](https://arxiv.org/abs/2610.02200) with image archives, calibrated coordinates, regional inspection, physical-value readout, profiles, Fourier analysis, feature localization, and registered image differences. These tools let a general-purpose multimodal model work with experimental evidence throughout an external-agent session.

| Project | Role |
|---|---|
| **MAST** | Instrument backend for external agents: skills, execution constraints, state and observation access, action records, and operator interaction |
| [VISTA-SPM](https://github.com/HamsterPark/VISTA-SPM) | Integrated visual harness and Python reference modules for retaining, revisiting, and measuring SPM observations with a general-purpose multimodal model |
| [dsh-spm](https://github.com/HamsterPark/dsh-spm) | SPM instruments and scientific tools in the DeepSeek Harness runtime, including TypeScript implementations of visual-memory and measurement capabilities |
| [STM-Bench](https://github.com/HamsterPark/STM-Bench) | Simulated instruments, scientific tasks, acquisition records, and evidence-based evaluation of experimental strategies |

A target workflow is to compare scans before and after atom manipulation, register them against nearby landmarks, measure the displacement, and choose a follow-up observation that tests the interpretation. Visual modules also support offline analysis of archived scans and adaptation to other agent hosts.

VISTA-SPM provides a public reference implementation. Its capabilities are integrated into MAST and have also been ported to dsh-spm. Its simulator results and integration records are documented in the respective repositories. The **MAST 6.5.0** source snapshot predates this change in direction; its historical vision code and validation records remain version-specific.

## References

The references provide scientific background and related work. MAST-specific evidence is documented in the experimental records and validation sections above.

<details>
<summary>Full bibliography and introductory resources</summary>

1. Binnig et al. (1982), [Surface Studies by Scanning Tunneling Microscopy](https://doi.org/10.1103/PhysRevLett.49.57). An early experimental STM paper.
2. Tersoff and Hamann (1985), [Theory of the scanning tunneling microscope](https://doi.org/10.1103/PhysRevB.31.805). The theoretical connection between imaging and local electronic states, with its underlying approximations.
3. Song et al. (2010), [A 10 mK Scanning Probe Microscopy Facility](https://www.nist.gov/publications/10-mk-scanning-probe-microscopy-facility). Ultralow temperature, UHV, and junction stability.
4. Schwenk et al. (2020), [Achieving µeV tunneling resolution in an in-operando scanning tunneling microscopy, atomic force microscopy, and magnetotransport system for quantum materials research](https://doi.org/10.1063/5.0005320). Thermalization, filtering, and high-resolution spectroscopy.
5. Yothers et al. (2017), [Real-space post-processing correction of thermal drift and piezoelectric actuator nonlinearities in scanning tunneling microscope images](https://doi.org/10.1063/1.4974271). History dependence and correction of piezoelectric scanners.
6. Chen et al. (2022), [Precise atom manipulation through deep reinforcement learning](https://www.nature.com/articles/s41467-022-35149-w). Manipulation and atom assembly in a real STM.
7. Okuyama et al. (2025), [Integrated AI Framework for Room-Temperature Atom Manipulation in Scanning Probe Microscopy](https://doi.org/10.1021/acs.nanolett.5c04982). Integration of multiple perception and control modules.
8. Narasimha et al. (2026), [Self-Driving Scanning Probe Microscopy: From Acceleration to Discovery and Manipulation](https://doi.org/10.1021/acs.accounts.6c00273). A review of directions in autonomous scanning probe research.
9. Li et al. (2026), [Artificial intelligence-empowered scanning probe microscopy: Recent advances and future perspectives](https://doi.org/10.1016/j.asi.2026.100003). Expert knowledge, autonomous tip conditioning, and closed-loop experiments.
10. [Electrons, Photons, and Force: Quantitative Single-Molecule Measurements from Physics to Biology](https://doi.org/10.1021/nn103298x) (2011). Includes STM measurement principles and typical current scales.
11. [Electrostatic sample-tip interactions in the scanning tunneling microscope](https://doi.org/10.1103/PhysRevLett.70.2471) (1993). An experimental study of tip-induced band bending.
12. Wikipedia, [Scanning tunneling microscope](https://en.wikipedia.org/wiki/Scanning_tunneling_microscope). An introductory index of terminology and principles.
13. Han et al. (2026), [VISTA: A Visual Harness for Reasoning in an Interactive World](https://arxiv.org/abs/2610.02200). Persistent visual evidence, active inspection, and long-horizon multimodal reasoning.

</details>

## License and policies

[MIT license](LICENSE) · [Third-party notices](THIRD_PARTY.md) · [Contributing](CONTRIBUTING.md) · [Security policy](SECURITY.md)
