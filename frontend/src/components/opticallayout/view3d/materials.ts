// Materials of the 3D view.
//
// Parts are matte (Lambert): the hardware should read by its shape and colour, and a cheap material keeps the
// frame time down where the browser draws in software (docs/v2/design/optical_layout_3d_view.md section 6).
// Glass is the same material, see-through and without depth writes.
//
// Beams are tubes whose width on the SCREEN is the width of the 2D line of the same power (colors.ts beamLook):
// an instance carries its 2D width in pixels, and the vertex shader turns pixels into millimetres at the depth of
// each point of the tube's axis, so a beam stays as heavy as on the 2D canvas however far it is (and the light
// of a branch reads the same in both views). The dash patterns of the 2D lines (ultraviolet, infrared, no light)
// are cut out in the fragment shader along the tube, in pixels too.

import { DoubleSide, InstancedBufferAttribute, LineBasicMaterial, MeshBasicMaterial, MeshLambertMaterial, type InstancedMesh, type Material, type WebGLProgramParametersWithUniforms } from "three";

/** Parts: matte, coloured per vertex (merged.ts writes each instance's colour into its vertices). */
export function solidMaterial(): MeshLambertMaterial {
  return new MeshLambertMaterial({ color: 0xffffff, vertexColors: true });
}

/**
 * Glass: see-through, no depth writes, front faces only. The glass parts are closed and convex (lenses, plates,
 * cubes): their back faces would add a second, sorted pass (three draws double-sided transparency twice) for a
 * look one cannot tell apart at these sizes.
 */
export function glassMaterial(opacity: number): MeshLambertMaterial {
  return new MeshLambertMaterial({ color: 0xffffff, vertexColors: true, transparent: true, opacity, depthWrite: false });
}

/** The uniforms every beam tube shares: how many mm one pixel is, per mm of depth (perspective) or flat (orthographic). */
export interface BeamUniforms {
  uMmPerPxPerDepth: { value: number };
  uOrthoMmPerPx: { value: number };
  uOrtho: { value: number };
}

export function beamUniforms(): BeamUniforms {
  return { uMmPerPxPerDepth: { value: 0.001 }, uOrthoMmPerPx: { value: 1 }, uOrtho: { value: 0 } };
}

/** The attribute of a beam instance: radius (px), dash on (px), dash off (px), length (mm). */
export const BEAM_ATTRIBUTE = "aBeam";

const BEAM_VERTEX_HEAD = /* glsl */ `
attribute vec4 aBeam;
uniform float uMmPerPxPerDepth;
uniform float uOrthoMmPerPx;
uniform float uOrtho;
varying float vAlong;
varying vec2 vDash;
`;

const BEAM_VERTEX_BODY = /* glsl */ `
{
  vec4 axisView = modelViewMatrix * instanceMatrix * vec4(0.0, 0.0, position.z, 1.0);
  float mmPerPx = uOrtho > 0.5 ? uOrthoMmPerPx : max(-axisView.z, 1e-3) * uMmPerPxPerDepth;
  transformed.xy *= aBeam.x * mmPerPx;
  vAlong = position.z * aBeam.w;
  vDash = aBeam.yz * mmPerPx;
}
`;

const BEAM_FRAGMENT_HEAD = /* glsl */ `
varying float vAlong;
varying vec2 vDash;
`;

const BEAM_FRAGMENT_CUT = /* glsl */ `
  if (vDash.x + vDash.y > 0.0 && mod(vAlong, vDash.x + vDash.y) > vDash.x) discard;
`;

/** The material of the beam tubes (unlit: light is not lit by the room). Only for instanced meshes with `aBeam`. */
export function beamMaterial(u: BeamUniforms): MeshBasicMaterial {
  const m = new MeshBasicMaterial({ color: 0xffffff });
  m.onBeforeCompile = (shader: WebGLProgramParametersWithUniforms) => {
    shader.uniforms["uMmPerPxPerDepth"] = u.uMmPerPxPerDepth;
    shader.uniforms["uOrthoMmPerPx"] = u.uOrthoMmPerPx;
    shader.uniforms["uOrtho"] = u.uOrtho;
    shader.vertexShader = shader.vertexShader.replace("#include <common>", `#include <common>\n${BEAM_VERTEX_HEAD}`).replace("#include <begin_vertex>", `#include <begin_vertex>\n${BEAM_VERTEX_BODY}`);
    shader.fragmentShader = shader.fragmentShader.replace("#include <common>", `#include <common>\n${BEAM_FRAGMENT_HEAD}`).replace("void main() {", `void main() {\n${BEAM_FRAGMENT_CUT}`);
  };
  m.customProgramCacheKey = () => "mast-ol3d-beam-v1";
  return m;
}

/** Gives an instanced beam mesh its per-instance attribute (radius px, dash on px, dash off px, length mm). */
export function ensureBeamAttribute(mesh: InstancedMesh, capacity: number): InstancedBufferAttribute {
  const cur = mesh.geometry.getAttribute(BEAM_ATTRIBUTE) as InstancedBufferAttribute | undefined;
  if (cur && cur.count >= capacity) return cur;
  const attr = new InstancedBufferAttribute(new Float32Array(capacity * 4), 4);
  mesh.geometry.setAttribute(BEAM_ATTRIBUTE, attr);
  return attr;
}

/** A translucent surface coloured per vertex, alpha included (the envelope). */
export function envelopeMaterial(): MeshBasicMaterial {
  return new MeshBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false, side: DoubleSide });
}

export function linesMaterial(onTop = false): LineBasicMaterial {
  return new LineBasicMaterial({ vertexColors: true, depthTest: !onTop, transparent: onTop, opacity: onTop ? 0.95 : 1 });
}

export function disposeMaterial(m: Material | Material[]): void {
  for (const x of Array.isArray(m) ? m : [m]) x.dispose();
}
