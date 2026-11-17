# Contributing skills

A skill you build for your own session can also become part of MAST for everyone else, through the
community contribution tree, `contrib/skills/`. This file walks through that workflow; for the spec
format and the proposal mechanics themselves, see [Authoring skills](05-authoring-skills.md).

## The habit: keep what worked, locally first

If real work with MAST makes you write a procedure or tool that proved useful — it ran on real data
or on the instrument and gave the right answer — capture it as a reusable skill before the session
ends, and tell the user. Do not wait to be asked.

1. **Pick the form.** If official skills can express it, make a composite (`mast_composite_save`,
   stored in `config/composite_skills/`). Otherwise a Python skill.
2. **Save it locally by default.** For Python, scaffold and check a file in the local store:

   ```bash
   python scripts/skill_scaffold.py new MySkill --summary "one line" --evidence "what validated it"
   python scripts/skill_scaffold.py check MySkill
   ```

   (or send the code with `mast_propose_skill` / `POST /skills/proposals`; same folder, same checks).
   The file is `config/custom_skills/MySkill.py`: the file name, the class name, `metadata().name`
   and the `enabled.json` key are one CamelCase word, one skill class per file. The folder is
   gitignored, and nothing in it runs until the operator lists the name in
   `config/custom_skills/enabled.json` and restarts MAST. `check` reports what would stop it from
   loading (the loader itself only writes a log line): deny-list, shape, file name, units and
   bounds, footprint, a smoke run, and whether the evidence line is still a placeholder. Composites
   saved this way land in `config/composite_skills/`, which also holds tracked files: stage by
   explicit path, never `git add -A`.
3. **Say what validated it.** Fill the `Validated on` line in the docstring (the data set, frame
   or instrument run) and tell the user what you saved, where, and on what evidence.
4. **A pull request is the user's choice.** Follow the preference they already stated; otherwise ask
   once. To promote: copy it to `contrib/skills/<Name>/` as described below, run
   `python scripts/skill_check.py contrib/skills/<Name>`, and open the PR.
5. **A captured skill never bypasses a gate.** Declare the honest `safety_level` and capability
   tags; the operating-mode, token and ownership gates apply to it like to any other skill, and
   the compliance checks refuse a skill that hides its hardware footprint.

## The two tiers

| Tier | Verified on a real instrument? | Where it lives |
|---|---|---|
| Community | No — it passes the automated checks and its own tests; every user installs and enables it explicitly | `contrib/skills/<Name>/` |
| Official | Yes, by a maintainer | the built-in skill tree |

A community contribution is never auto-discovered and never shipped enabled: nothing in the
community tier runs on anyone's instrument until that person deliberately turns it on, the same way
a proposed Python skill only runs after the operator reviews and enables it.

## What a contribution looks like

Each contribution is one directory, `contrib/skills/<Name>/`, holding a `manifest.json` (its
name, safety level, footprint, authors, license and verification level), exactly one of `skill.py`
or `spec.json`, and at least one test file. Prefer a spec over Python source whenever the job can
be expressed as a `CompositeSpec`: it is data, not code, no new execution path is added, and its
safety level and capability tags are inherited from the official skills it steps through rather
than asserted by hand — see [Authoring skills](05-authoring-skills.md) for the format itself. Write
Python only when no combination of official skills can do what you need.

## Checking your work

The same compliance checks a proposal's reply carries run from the command line against a checkout,
so you can iterate locally before opening a pull request:

```bash
export PYTHONPATH=$PWD/MASTv2
python scripts/skill_check.py contrib/skills/<Name>
python -m pytest contrib -q
```

Exit code `0` means every check passed; `1` means at least one failed. A check the tool could not
run at all (for example, because an optional dependency is missing) is reported separately, and it
does not count as passing — install what is missing and run it again rather than treating a skipped
check as a green light.

## What is not accepted

| Not accepted | Why |
|---|---|
| A port of a published paper's method, or code copied from someone else's repository | This tree is for original work; the checks also reject citations, DOIs and arXiv identifiers found in the source. |
| A default that describes one particular instrument (a calibration, a piezo range, a controller gain, a measured working point) | Defaults must be instrument-independent, or left unset with explicit bounds instead. |
| Other changes bundled into a skill pull request | Keep a skill pull request to `contrib/skills/<Name>/`; send changes elsewhere as their own pull request (see `CONTRIBUTING.md` in the repository root), where they are merged, imported into the development repository, tested there and shipped in the next release. |

## Licensing and sign-off

Contributions are accepted under the MIT license, the same license as the rest of the project, and
each commit needs a sign-off (`git commit -s`) certifying the
[Developer Certificate of Origin](https://developercertificate.org/): that you wrote the
contribution, or otherwise have the right to submit it under that license. Your contribution may
also be included in closed-source or commercial distributions built from this project — the MIT
license permits that, and submitting means you agree to it. If that is not acceptable to you,
please do not submit.

## Graduation

A community skill graduates once a maintainer has run it on a real instrument and verified it
there. It then moves out of `contrib/` into the official tree, its major version is bumped — from
that point it is held to the official standard, not the community one — and it is recorded in
`contrib/GRADUATED.md` so the credit for it stays with whoever wrote it.
