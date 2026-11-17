// The live 3D view without React: the renderer, the two cameras (perspective and orthographic), the orbit
// controls, the CSS2D label layer and the scene (LayoutScene3D), drawn on demand: a frame is rendered only when
// the camera moves (OrbitControls "change", which damping keeps firing until it settles) or the scene changes.
// View3D.tsx owns one and feeds it the editor's props.

import { OrthographicCamera, PerspectiveCamera, Raycaster, Vector2, Vector3, WebGLRenderer } from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { CSS2DRenderer } from "three/examples/jsm/renderers/CSS2DRenderer.js";
import type { ThemeName } from "@/lib/opticalLayout/colors";
import { boxCentre, boxRadius, fitBoxDistance, orthoBoxSpan, presetDirection, type ViewPreset } from "@/lib/opticalLayout/scene3d/views";
import { rayAtHeight } from "@/lib/opticalLayout/scene3d/pick";
import { pickTube } from "@/lib/opticalLayout/scene3d/screenPick";
import type { Vec3 } from "@/lib/opticalLayout/scene3d/mat4";
import type { TubeSpec } from "@/lib/opticalLayout/scene3d/beams3d";
import { LayoutScene3D, type SceneInput } from "./LayoutScene3D";
import { SceneHolder } from "./sceneHolder";
import { snapshotPng, type LabelShot } from "./exporters";

export type Projection = "perspective" | "orthographic";

/** Vertical field of view of the perspective camera, degrees. */
export const FOV_DEG = 40;
/** The most device pixels per CSS pixel the view renders (a 4K screen at 3× would be 9× the work for little gain). */
export const MAX_PIXEL_RATIO = 2;

export interface EngineOptions {
  /**
   * Draw cheaply (a software renderer, files.ts isSoftwareRenderer): no multisampling, one device pixel per CSS
   * pixel, and coarser round shapes while the camera moves (the fine ones come back DETAIL_SETTLE_MS after it stops).
   */
  low: boolean;
}

/** How long after the camera stops the fine shapes come back (software renderer only), ms. */
export const DETAIL_SETTLE_MS = 200;

export interface EngineHooks {
  /** the WebGL context was lost (driver reset, too many contexts): the view says so */
  onContextLost: () => void;
  /** the browser gave the context back: three.js uploads everything again on the next frame */
  onContextRestored?: () => void;
  /** after every frame: where the orbit target is (the editor's "centre of the view") */
  onTarget?: (x: number, y: number) => void;
}

export class Engine {
  readonly renderer: WebGLRenderer;
  readonly labelRenderer = new CSS2DRenderer();
  /** the scene lives in a holder: a lost WebGL context replaces it (sceneHolder.ts) */
  readonly holder: SceneHolder<LayoutScene3D>;
  get content(): LayoutScene3D {
    return this.holder.current;
  }
  readonly persp = new PerspectiveCamera(FOV_DEG, 1, 1, 1e5);
  readonly ortho = new OrthographicCamera(-1, 1, 1, -1, 0.1, 1e5);
  camera: PerspectiveCamera | OrthographicCamera;
  readonly controls: OrbitControls;
  width = 1;
  height = 1;
  private raf = 0;
  private selected: ReadonlySet<string> = new Set();
  private hover: string | null = null;
  private fitted = false;
  private hasContent = false;
  private readonly raycaster = new Raycaster();
  private disposed = false;
  /** filled while measuring (window.__ol3d): how long each frame's work took, ms */
  frameWork: number[] | null = null;

  constructor(
    readonly host: HTMLElement,
    theme: ThemeName,
    private readonly hooks: EngineHooks,
    readonly options: EngineOptions = { low: false },
  ) {
    this.renderer = new WebGLRenderer({ antialias: !options.low, powerPreference: "high-performance" });
    this.renderer.setPixelRatio(options.low ? 1 : Math.min(MAX_PIXEL_RATIO, window.devicePixelRatio || 1));
    const canvas = this.renderer.domElement;
    canvas.style.display = "block";
    canvas.style.width = "100%";
    canvas.style.height = "100%";
    canvas.dataset["ol3dCanvas"] = "";
    canvas.addEventListener("webglcontextlost", this.onLost, false);
    canvas.addEventListener("webglcontextrestored", this.onRestored, false);
    host.appendChild(canvas);
    const lab = this.labelRenderer.domElement;
    lab.style.position = "absolute";
    lab.style.inset = "0";
    lab.style.pointerEvents = "none";
    lab.dataset["ol3dLabels"] = "";
    host.appendChild(lab);

    this.persp.up.set(0, 0, 1);
    this.ortho.up.set(0, 0, 1);
    this.camera = this.persp;
    this.controls = new OrbitControls(this.camera, canvas);
    this.controls.enableDamping = true;
    this.controls.dampingFactor = 0.14;
    this.controls.screenSpacePanning = true;
    this.controls.zoomToCursor = true;
    this.controls.addEventListener("change", this.onCameraChange);
    // the holes seen at a slant stay round with anisotropic filtering; a software renderer pays for it per pixel
    const anisotropy = options.low ? 1 : Math.min(8, this.renderer.capabilities.getMaxAnisotropy());
    this.holder = new SceneHolder(() => new LayoutScene3D(theme, { anisotropy }));
  }

  private settleTimer = 0;

  /** The camera moved: draw a frame; on a software renderer, with the coarse shapes until it has stopped. */
  private onCameraChange = (): void => {
    if (this.options.low) {
      this.content.setDetail("low");
      window.clearTimeout(this.settleTimer);
      this.settleTimer = window.setTimeout(() => {
        this.content.setDetail("high");
        this.requestRender();
      }, DETAIL_SETTLE_MS);
    }
    this.requestRender();
  };

  private onLost = (e: Event) => {
    // preventDefault: the browser may give the context back (webglcontextrestored)
    e.preventDefault();
    this.holder.contextLost();
    this.hooks.onContextLost();
  };

  private onRestored = () => {
    this.holder.contextRestored();
    this.hooks.onContextRestored?.();
    this.requestRender();
  };

  // ── content ────────────────────────────────────────────────────────────────

  update(input: SceneInput): void {
    this.selected = input.selected;
    this.hover = input.hover;
    this.holder.update(input);
    this.hasContent = true;
    this.renderer.setClearColor(this.content.colors.background, 1);
    this.fitOnce();
    this.requestRender();
  }

  /** The first view, once there is something to look at and a size to look at it in. */
  private fitOnce(): void {
    if (this.fitted || !this.hasContent || this.width < 2 || this.height < 2) return;
    this.fitted = true;
    this.setView("iso");
  }

  setSize(w: number, h: number): void {
    this.width = Math.max(1, Math.floor(w));
    this.height = Math.max(1, Math.floor(h));
    this.renderer.setSize(this.width, this.height, false);
    this.labelRenderer.setSize(this.width, this.height);
    this.persp.aspect = this.width / this.height;
    this.persp.updateProjectionMatrix();
    this.setOrthoFrustum(this.orthoHeight());
    this.fitOnce();
    this.requestRender();
  }

  // ── the camera ─────────────────────────────────────────────────────────────

  private orthoHeight(): number {
    return this.ortho.top - this.ortho.bottom;
  }

  private setOrthoFrustum(span: number): void {
    const aspect = this.width / this.height;
    this.ortho.top = span / 2;
    this.ortho.bottom = -span / 2;
    this.ortho.left = (-span / 2) * aspect;
    this.ortho.right = (span / 2) * aspect;
    this.ortho.updateProjectionMatrix();
  }

  /** One of the preset views, holding everything that is drawn. */
  setView(preset: ViewPreset): void {
    this.lookFrom(presetDirection(preset));
  }

  /** Fits everything in, looking from where the camera looks now. */
  fit(): void {
    const d = this.camera.position.clone().sub(this.controls.target).normalize();
    this.lookFrom(d.lengthSq() > 0 ? [d.x, d.y, d.z] : presetDirection("iso"));
  }

  /** Looks at the middle of everything from `dir` (unit, from the target towards the camera), just far enough to hold it all. */
  private lookFrom(dir: [number, number, number]): void {
    const box = this.content.box;
    const target = boxCentre(box);
    const aspect = this.width / this.height;
    this.controls.target.set(...target);
    if (this.camera === this.persp) {
      const d = fitBoxDistance(box, dir, FOV_DEG, aspect);
      this.persp.position.set(target[0] + dir[0] * d, target[1] + dir[1] * d, target[2] + dir[2] * d);
    } else {
      // an orthographic camera sees the same at any distance: far enough to have the whole box in front of it
      const r = boxRadius(box) * 2;
      this.ortho.position.set(target[0] + dir[0] * r, target[1] + dir[1] * r, target[2] + dir[2] * r);
      this.ortho.zoom = 1;
      this.setOrthoFrustum(orthoBoxSpan(box, dir, aspect));
    }
    this.camera.lookAt(this.controls.target);
    this.controls.update();
    this.requestRender();
  }

  setProjection(kind: Projection): void {
    const from = this.camera;
    const to = kind === "perspective" ? this.persp : this.ortho;
    if (from === to) return;
    const target = this.controls.target;
    const dist = from.position.distanceTo(target);
    if (to === this.ortho) {
      // the same picture size at the target: the height the perspective camera sees there
      this.setOrthoFrustum(2 * dist * Math.tan((FOV_DEG * Math.PI) / 360));
      this.ortho.zoom = 1;
      this.ortho.position.copy(from.position);
      const r = boxRadius(this.content.box) * 2;
      if (dist < r) this.ortho.position.copy(target).addScaledVector(from.position.clone().sub(target).normalize(), r);
    } else {
      const span = this.orthoHeight() / this.ortho.zoom;
      const d = span / (2 * Math.tan((FOV_DEG * Math.PI) / 360));
      this.persp.position.copy(target).addScaledVector(from.position.clone().sub(target).normalize(), d);
    }
    to.lookAt(target);
    this.camera = to;
    this.controls.object = to;
    this.controls.update();
    this.requestRender();
  }

  /** Moves the view to a point of the table, keeping the direction; with `minPxPerMm`, zooms in at least that far. */
  focusOn(x: number, y: number, z: number, minPxPerMm = 0): void {
    const target = new Vector3(x, y, z);
    const offset = this.camera.position.clone().sub(this.controls.target);
    if (minPxPerMm > 0) {
      if (this.camera === this.persp) {
        const maxDist = this.height / (minPxPerMm * 2 * Math.tan((FOV_DEG * Math.PI) / 360));
        if (offset.length() > maxDist) offset.setLength(maxDist);
      } else {
        const zoom = (minPxPerMm * this.orthoHeight()) / this.height;
        if (this.ortho.zoom < zoom) {
          this.ortho.zoom = zoom;
          this.ortho.updateProjectionMatrix();
        }
      }
    }
    this.controls.target.copy(target);
    this.camera.position.copy(target).add(offset);
    this.controls.update();
    this.requestRender();
  }

  /** Millimetres per pixel at the orbit target (for the beam pick tolerance and the beam width). */
  mmPerPxAtTarget(): number {
    if (this.camera === this.persp) return (2 * this.persp.position.distanceTo(this.controls.target) * Math.tan((FOV_DEG * Math.PI) / 360)) / this.height;
    return this.orthoHeight() / this.ortho.zoom / this.height;
  }

  // ── drawing ────────────────────────────────────────────────────────────────

  requestRender = (): void => {
    if (this.raf || this.disposed) return;
    this.raf = requestAnimationFrame(this.frame);
  };

  private frame = (): void => {
    this.raf = 0;
    if (this.disposed) return;
    const t0 = performance.now();
    // damping: update() moves the camera on and fires "change" (another frame) until it settles
    this.controls.update();
    this.draw();
    if (this.frameWork) this.frameWork.push(performance.now() - t0);
    const t = this.controls.target;
    this.hooks.onTarget?.(t.x, t.y);
  };

  /** Renders the scene and the labels once, now. */
  draw(): void {
    const cam = this.camera;
    const box = this.content.box;
    const radius = Math.max(100, boxRadius(box));
    if (cam === this.persp) {
      const dist = Math.max(1, this.persp.position.distanceTo(this.controls.target));
      const near = Math.min(50, Math.max(0.05, dist / 400));
      const far = dist + radius * 6;
      if (this.persp.near !== near || this.persp.far !== far) {
        this.persp.near = near;
        this.persp.far = far;
        this.persp.updateProjectionMatrix();
      }
      this.content.setPixelScale("perspective", (2 * Math.tan((FOV_DEG * Math.PI) / 360)) / this.height);
    } else {
      const far = this.ortho.position.distanceTo(this.controls.target) + radius * 6;
      if (this.ortho.far !== far) {
        this.ortho.far = far;
        this.ortho.updateProjectionMatrix();
      }
      this.content.setPixelScale("orthographic", this.orthoHeight() / this.ortho.zoom / this.height);
    }
    this.renderer.render(this.content.scene, cam);
    this.content.declutterLabels(cam, this.width, this.height, this.selected, this.hover);
    this.labelRenderer.render(this.content.labelScene, cam);
  }

  // ── picking ────────────────────────────────────────────────────────────────

  private rayAt(clientX: number, clientY: number): Raycaster {
    const r = this.renderer.domElement.getBoundingClientRect();
    const ndc = new Vector2(((clientX - r.left) / r.width) * 2 - 1, -((clientY - r.top) / r.height) * 2 + 1);
    this.raycaster.setFromCamera(ndc, this.camera);
    return this.raycaster;
  }

  pickComponent(clientX: number, clientY: number): { id: string; point: Vec3 } | null {
    return this.content.pickComponent(this.rayAt(clientX, clientY));
  }

  /** Where the pointer's ray meets the level plane z = h (null near the horizon). */
  pointAtHeight(clientX: number, clientY: number, h: number): Vec3 | null {
    const ray = this.rayAt(clientX, clientY).ray;
    return rayAtHeight([ray.origin.x, ray.origin.y, ray.origin.z], [ray.direction.x, ray.direction.y, ray.direction.z], h);
  }

  /** The drawn beam within `maxPx` of the pointer, and the point on it. */
  pickBeam(clientX: number, clientY: number, maxPx: number): { tube: TubeSpec; point: Vec3 } | null {
    const r = this.renderer.domElement.getBoundingClientRect();
    const px = clientX - r.left;
    const py = clientY - r.top;
    const v = new Vector3();
    const toScreen = (p: Vec3): [number, number] | null => {
      v.set(p[0], p[1], p[2]).project(this.camera);
      if (v.z < -1 || v.z > 1) return null;
      return [(v.x * 0.5 + 0.5) * this.width, (-v.y * 0.5 + 0.5) * this.height];
    };
    const hit = pickTube(this.content.tubes, toScreen, px, py, maxPx);
    return hit ? { tube: hit.tube, point: hit.point } : null;
  }

  /** Where a component's centre (on its optical axis) is in the page, CSS px; null when it is not drawn or behind. */
  screenOf(id: string): { x: number; y: number } | null {
    const n = this.content.node(id);
    return n ? this.pageOf([n.component.x_mm, n.component.y_mm, n.z]) : null;
  }

  /** Where a point of the table's frame is in the page, CSS px; null when it is behind the camera. */
  pageOf(p: Vec3): { x: number; y: number } | null {
    const v = new Vector3(p[0], p[1], p[2]).project(this.camera);
    if (v.z < -1 || v.z > 1) return null;
    const r = this.renderer.domElement.getBoundingClientRect();
    return { x: r.left + (v.x * 0.5 + 0.5) * r.width, y: r.top + (-v.y * 0.5 + 0.5) * r.height };
  }

  // ── exports ────────────────────────────────────────────────────────────────

  /** The labels on the screen now, for the PNG. */
  labelShots(): LabelShot[] {
    const out: LabelShot[] = [];
    const v = new Vector3();
    const cam = this.camera;
    for (const o of this.content.visibleLabels()) {
      const el = o.element;
      if (!el.textContent) continue;
      v.copy(o.position).project(cam);
      const cs = getComputedStyle(el);
      out.push({
        text: el.textContent,
        x: (v.x * 0.5 + 0.5) * this.width,
        y: (-v.y * 0.5 + 0.5) * this.height,
        cx: o.center.x,
        cy: o.center.y,
        font: cs.font,
        color: cs.color,
        background: cs.backgroundColor,
        w: el.offsetWidth,
        h: el.offsetHeight,
      });
    }
    return out;
  }

  snapshot(scale: number): Promise<{ blob: Blob; scale: number }> {
    return snapshotPng(this.renderer, () => this.draw(), this.width, this.height, scale, this.labelShots());
  }

  // ── cleaning up ────────────────────────────────────────────────────────────

  dispose(): void {
    this.disposed = true;
    if (this.raf) cancelAnimationFrame(this.raf);
    this.controls.removeEventListener("change", this.onCameraChange);
    window.clearTimeout(this.settleTimer);
    this.controls.dispose();
    this.holder.dispose();
    this.renderer.domElement.removeEventListener("webglcontextlost", this.onLost, false);
    this.renderer.domElement.removeEventListener("webglcontextrestored", this.onRestored, false);
    this.renderer.dispose();
    // give the context back now rather than when the canvas is collected: a browser keeps only about sixteen, and
    // switching between 2D and 3D twenty times would otherwise lose the oldest ones (and say so in the console)
    this.renderer.forceContextLoss();
    this.renderer.domElement.remove();
    this.labelRenderer.domElement.remove();
  }
}
