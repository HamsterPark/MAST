/**
 * How to name the "currently effective library"; pure, runnable under `node --test`.
 *
 * Why it is needed: the effective library id is derived deterministically from the
 * experiment id (`exp_<id8>`), and `resolve_effective_library(ensure=False)`
 * deliberately does not create the library, since a read path must not have side
 * effects (see the docstring in experiment_library.py). Until the experiment first
 * ingests a paper, `effective_library_id` therefore points at a library that is not in
 * the registry yet: `GET /api/literature/libraries` returns an `effective_library_id`
 * of `exp_<id8>` while `libraries` holds only `reading`.
 *
 * Three views look the name up with `libraries.find(...)` and fall back to the bare id,
 * so the user saw only that id. The name is available, though: it is the current
 * experiment's name (what `experiment_library._default_name()` uses when the library is
 * actually created).
 */
import { tr } from "./i18n.ts";

export interface LibraryRowLike {
  library_id: string;
  name?: string | null;
  experiment_name?: string | null;
}

export interface LibraryLabel {
  /** Human-readable name. Never empty: the worst case is the bare id. */
  text: string;
  /**
   * The library has not been created yet (lazy creation: it is written on first
   * ingest). When true the UI should say so rather than pretend it exists: the name is
   * derived, the library is not.
   */
  pending: boolean;
}

/**
 * @param libraryId       effective library id (or the id explicitly picked in the panel)
 * @param source          "experiment" | "manual" | "fallback" | "picked"
 * @param libraries       the libraries array of `/api/literature/libraries`
 * @param experimentName  current experiment name (`/api/experiments/current`), may be empty
 */
export function libraryLabel(
  libraryId: string,
  source: string,
  libraries: readonly LibraryRowLike[],
  experimentName?: string | null,
): LibraryLabel {
  const id = (libraryId || "").trim();
  if (!id) return { text: "", pending: false };

  const row = libraries.find((l) => l.library_id === id) ?? null;
  if (row) {
    // An experiment library prefers the experiment name; a bare exp_1a2b3c4d says nothing.
    const exp = (row.experiment_name || "").trim();
    if (exp) return { text: tr(`《${exp}》的文献库`, `Library of "${exp}"`), pending: false };
    const name = (row.name || "").trim();
    if (name) return { text: name, pending: false };
    return { text: id, pending: false };
  }

  // The library is not in the list. The only non-error case is an experiment library
  // that has not been lazily created yet: the name can be derived from the current
  // experiment, but it must be marked as not yet on disk.
  const exp = (experimentName || "").trim();
  if (source === "experiment" && exp) {
    return { text: tr(`《${exp}》的文献库`, `Library of "${exp}"`), pending: true };
  }
  return { text: id, pending: source === "experiment" };
}
