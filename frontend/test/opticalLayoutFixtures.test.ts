// ════════════════════════════════════════════════════════════════════════════
// The shared trace fixtures: tests/fixtures/optical_layout/*.json
//
//     cd frontend && npm run test:unit
//
// The backend traces in Python (MASTv2/mast/optical_layout/trace.py), the editor traces in
// TypeScript (src/lib/opticalLayout/trace.ts), and both read the same files: each holds a
// layout and the trace the Python implementation produced for it, checked against the
// physics by hand. The two implementations must agree on every one, so a rule that
// changes on one side turns the other side red. This file is the TypeScript half of that
// pact; tests/v2/unit/optical_layout/test_layout_trace_fixtures.py is the Python half.
//
// Besides the trace, the fixtures are the best available specimens of "a layout the server
// accepts", so they also check the client side of the contract: the parser reads them, the
// early validation finds no error in them (it must never be stricter than the server), and
// the editor's own normalisation leaves them as they are (same rounding as the server).
//
// The directory is found by walking up from this file to the repository root. A missing or
// emptied directory is a failure, not a skip: a pact with nothing in it proves nothing.
// ════════════════════════════════════════════════════════════════════════════

import assert from "node:assert/strict";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import { describe, it } from "node:test";

import { TRACE_LIMITS, traceLayout, traceLayoutDetailed } from "../src/lib/opticalLayout/trace.ts";
import { parseLayout } from "../src/lib/opticalLayout/parse.ts";
import { errorsOf, validateLayout } from "../src/lib/opticalLayout/validate.ts";
import { normalizeGeometry } from "../src/lib/opticalLayout/edit.ts";
import type { OpticalLayout, Trace } from "../src/lib/opticalLayout/types.ts";
import { firstDifference } from "./helpers/opticalLayout.ts";

function findFixtureDir(): string {
  let dir = dirname(fileURLToPath(import.meta.url));
  for (let i = 0; i < 8; i++) {
    const candidate = join(dir, "tests", "fixtures", "optical_layout");
    if (existsSync(candidate)) return candidate;
    const up = dirname(dir);
    if (up === dir) break;
    dir = up;
  }
  throw new Error("tests/fixtures/optical_layout/ was not found above the frontend directory");
}

interface Fixture {
  name: string;
  description: string;
  layout: OpticalLayout;
  expected_trace: Trace;
  /** only the trace_budget_* fixtures: the (smaller) budget the expected trace was made with */
  trace_options?: { max_trace_events?: number };
  /** only the trace_budget_* fixtures: the contract's default budget, which both sides compare their constant to */
  default_max_trace_events?: number;
  file: string;
}

/** The options a fixture's trace is made with. */
const optionsOf = (f: Fixture) => (f.trace_options?.max_trace_events === undefined ? undefined : { maxTraceEvents: f.trace_options.max_trace_events });

const DIR = findFixtureDir();
const FILES = readdirSync(DIR).filter((f) => f.endsWith(".json")).sort();
const FIXTURES: Fixture[] = FILES.map((file) => ({ ...(JSON.parse(readFileSync(join(DIR, file), "utf8")) as Omit<Fixture, "file">), file }));

/** Fixtures the contract (section 4) and its README name; deleting one must be noticed. */
const REQUIRED = [
  "single_fold_mirror",
  "two_mirror_dogleg",
  "beam_splitter_two_branches",
  "dichroic_by_wavelength",
  "filter_blocks_and_passes",
  "back_side_hits",
  "off_center_hits",
  "miss_aperture",
  "fiber_coupling_continues",
  "leaves_table",
  "iris_open_diameter",
  "shutter_closed_and_open",
  "max_events_ping_pong",
  "trace_budget_exact",
  "trace_budget_cut",
  "nearest_hit_ties",
];

describe("the shared trace fixtures", () => {
  it("are all there", () => {
    assert.ok(FIXTURES.length >= 20, `only ${FIXTURES.length} fixtures in ${DIR}`);
    const names = new Set(FILES.map((f) => f.replace(/\.json$/, "")));
    const missing = REQUIRED.filter((r) => !names.has(r));
    assert.deepEqual(missing, [], `fixtures the contract names are missing: ${missing.join(", ")}`);
    for (const f of FIXTURES) {
      assert.ok(f.layout && Array.isArray(f.expected_trace), `${f.file}: needs a layout and an expected_trace`);
    }
  });

  for (const f of FIXTURES) {
    describe(f.file, () => {
      it("the TypeScript trace equals the Python trace (1e-6, keys and strings exactly)", () => {
        const actual = traceLayout(f.layout, optionsOf(f));
        const diff = firstDifference(actual, f.expected_trace, 1e-6);
        assert.equal(diff, null, `${f.file} (${f.description})\n${diff}`);
      });

      it("has the shape the contract gives a trace: one segment per event, one more for a branch that left the table or ran out of budget", () => {
        for (const tree of f.expected_trace) {
          for (const b of tree.branches) {
            const more = b.end === "left_table" || b.end === "budget" ? 1 : 0;
            assert.equal(b.segments.length, b.events.length + more, `${f.file} ${tree.source} branch ${b.branch} (${b.end})`);
          }
        }
        const cut = f.expected_trace.flatMap((t) => t.branches.filter((b) => b.end === "budget").map((b) => [t, b] as const));
        if (f.trace_options?.max_trace_events === undefined) {
          assert.equal(cut.length, 0, "a fixture without a budget of its own is never cut");
        } else if (cut.length > 0) {
          assert.equal(cut.length, 1, "exactly one branch ends with budget");
          const lastTree = f.expected_trace[f.expected_trace.length - 1]!;
          assert.equal(cut[0]![0], lastTree, "it is in the last tree");
          assert.equal(cut[0]![1], lastTree.branches[lastTree.branches.length - 1], "and it is that tree's last branch");
          const events = f.expected_trace.reduce((n, t) => n + t.branches.reduce((m, b) => m + b.events.length, 0), 0);
          assert.equal(events, f.trace_options.max_trace_events, "a cut trace holds exactly the budget");
        }
      });

      if (f.default_max_trace_events !== undefined) {
        it("the budget constant is the contract's", () => {
          assert.equal(TRACE_LIMITS.maxTraceEvents, f.default_max_trace_events);
        });
      }

      it("the editor reads it: parse, early validation without errors, normalisation unchanged", () => {
        const parsed = parseLayout(f.layout);
        assert.equal(parsed.components.length, f.layout.components.length);
        const errors = errorsOf(validateLayout(parsed));
        assert.deepEqual(errors, [], `the early check is stricter than the server on ${f.file}`);
        for (const c of parsed.components) {
          const n = normalizeGeometry(c);
          assert.deepEqual(
            { x: n.x_mm, y: n.y_mm, a: n.angle_deg, ap: n.aperture_mm },
            { x: c.x_mm, y: c.y_mm, a: c.angle_deg, ap: c.aperture_mm },
            `${f.file}/${c.id}: the editor would round a value the server keeps`,
          );
        }
      });

      it("the detailed trace (the one the editor draws) is the contract trace before rounding", () => {
        const detailed = traceLayoutDetailed(f.layout, optionsOf(f));
        const contract = traceLayout(f.layout, optionsOf(f));
        assert.equal(detailed.length, contract.length);
        detailed.forEach((s, i) => {
          assert.equal(s.source, contract[i]!.source);
          assert.equal(s.branches.length, contract[i]!.branches.length);
          s.branches.forEach((b, j) => {
            const c = contract[i]!.branches[j]!;
            assert.equal(b.branch, c.branch);
            assert.equal(b.end, c.end);
            assert.equal(b.segments.length, c.segments.length);
            b.segments.forEach((g, k) => {
              const h = c.segments[k]!;
              assert.ok(Math.abs(g.x1 - h.x1) <= 5.1e-4 && Math.abs(g.y1 - h.y1) <= 5.1e-4, `${f.file} ${b.branch} segment ${k}`);
            });
          });
        });
      });
    });
  }
});
