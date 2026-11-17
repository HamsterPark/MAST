// Holds the engine's scene across a lost WebGL context.
//
// When the browser takes the context away (webglcontextlost) every GPU object three.js made for the scene (buffers,
// textures, programs) is invalid, even after the context comes back: three.js starts over with a fresh set of
// internal tables, but the geometries, materials and textures we hold still carry "dispose" listeners that point at
// the OLD tables, so calling dispose() on them makes the browser warn "delete: object does not belong to this
// context" once per object. So while the context is lost the scene is marked stale, and it is never disposed;
// after the restore a new scene is built from the last input (the old one is only detached from the page). Disposal
// of the new scene, which belongs to the live context, is as usual.

import type { Detail } from "./geometries.ts";
import type { SceneInput } from "./LayoutScene3D.ts";

/** What the holder needs of a scene (LayoutScene3D has all of it). */
export interface HeldScene {
  update(input: SceneInput): void;
  setDetail(detail: Detail): void;
  getDetail(): Detail;
  /** free the GPU objects (only for a scene that belongs to the live context) */
  dispose(): void;
  /** let go of the scene without touching the GPU: its objects belong to a context that is gone */
  abandon(): void;
}

export class SceneHolder<S extends HeldScene> {
  private scene: S;
  private last: SceneInput | null = null;
  private lostNow = false;
  /** how many scenes were left stale by a lost context (for tests and the perf hook) */
  abandoned = 0;

  private readonly make: () => S;

  constructor(make: () => S) {
    this.make = make;
    this.scene = make();
  }

  get current(): S {
    return this.scene;
  }

  get lost(): boolean {
    return this.lostNow;
  }

  update(input: SceneInput): void {
    this.last = input;
    this.scene.update(input);
  }

  /** The context is gone: the scene's GPU objects are invalid from here on. */
  contextLost(): void {
    if (this.lostNow) return;
    this.lostNow = true;
    this.scene.abandon();
    this.abandoned++;
  }

  /** The context is back: a new scene, from what was shown last, with nothing carried over from the old GPU objects. */
  contextRestored(): void {
    if (!this.lostNow) return;
    this.lostNow = false;
    const detail = this.scene.getDetail();
    this.scene = this.make();
    this.scene.setDetail(detail);
    if (this.last) this.scene.update(this.last);
  }

  /** The view is closing. A scene that is already stale is not disposed (that is what produces the warnings). */
  dispose(): void {
    if (this.lostNow) return;
    this.scene.dispose();
  }
}
