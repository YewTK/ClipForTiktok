// Background: a field of points riding a noise wave, tinted cyan-to-red.
import * as THREE from 'https://cdn.jsdelivr.net/npm/three@0.170.0/build/three.module.js';

const canvas = document.getElementById('scene');
const still = matchMedia('(prefers-reduced-motion: reduce)').matches;
const renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true, powerPreference: 'high-performance' });
const pixelRatio = Math.min(devicePixelRatio, 2);
renderer.setPixelRatio(pixelRatio);

const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, 1, 0.1, 100);

const small = innerWidth < 700;
// dense along x, sparse along z, so the points read as flowing silk threads
const COLS = small ? 300 : 560, ROWS = small ? 64 : 84;
const W = small ? 22 : 40, D = 24;
const pos = new Float32Array(COLS * ROWS * 3);
for (let r = 0, i = 0; r < ROWS; r++) {
  for (let c = 0; c < COLS; c++, i += 3) {
    pos[i] = (c / (COLS - 1) - 0.5) * W;
    pos[i + 2] = (r / (ROWS - 1) - 0.5) * D - 5;
  }
}
const geometry = new THREE.BufferGeometry();
geometry.setAttribute('position', new THREE.BufferAttribute(pos, 3));

const uniforms = {
  uTime: { value: 0 }, uEnergy: { value: 1 }, uRipple: { value: 99 }, uPixel: { value: pixelRatio },
  uCyan: { value: new THREE.Color('#25f4ee') }, uRed: { value: new THREE.Color('#fe2c55') },
};
const material = new THREE.ShaderMaterial({
  uniforms, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
  vertexShader: /* glsl */ `
    uniform float uTime, uEnergy, uRipple, uPixel;
    varying float vHeight, vFade, vMix;

    vec3 permute(vec3 x) { return mod(((x * 34.0) + 1.0) * x, 289.0); }
    float snoise(vec2 v) {
      const vec4 C = vec4(0.211324865405187, 0.366025403784439, -0.577350269189626, 0.024390243902439);
      vec2 i = floor(v + dot(v, C.yy));
      vec2 x0 = v - i + dot(i, C.xx);
      vec2 i1 = (x0.x > x0.y) ? vec2(1.0, 0.0) : vec2(0.0, 1.0);
      vec4 x12 = x0.xyxy + C.xxzz;
      x12.xy -= i1;
      i = mod(i, 289.0);
      vec3 p = permute(permute(i.y + vec3(0.0, i1.y, 1.0)) + i.x + vec3(0.0, i1.x, 1.0));
      vec3 m = max(0.5 - vec3(dot(x0, x0), dot(x12.xy, x12.xy), dot(x12.zw, x12.zw)), 0.0);
      m = m * m; m = m * m;
      vec3 x = 2.0 * fract(p * C.www) - 1.0;
      vec3 h = abs(x) - 0.5;
      vec3 ox = floor(x + 0.5);
      vec3 a0 = x - ox;
      m *= 1.79284291400159 - 0.85373472095314 * (a0 * a0 + h * h);
      vec3 g;
      g.x = a0.x * x0.x + h.x * x0.y;
      g.yz = a0.yz * x12.xz + h.yz * x12.yw;
      return 130.0 * dot(m, g);
    }

    void main() {
      vec3 p = position;
      float t = uTime;
      float broad = snoise(vec2(p.x * 0.11 + t * 0.05, p.z * 0.14 - t * 0.09));
      float fine = snoise(vec2(p.x * 0.34 - t * 0.11, p.z * 0.4 + t * 0.07));
      float y = broad * 1.25 + fine * 0.32 * uEnergy;
      y += sin(p.x * 0.45 + t * 0.6) * 0.22 + sin(p.z * 0.7 - t * 0.8) * 0.18 * uEnergy;

      // success ripple spreading outward from under the card
      float d = length(p.xz - vec2(0.0, -1.0));
      float front = uRipple * 5.0;
      float ring = exp(-pow(d - front, 2.0) * 0.5) * max(0.0, 1.0 - uRipple / 3.5);
      y += ring * 1.6;

      p.y = y;
      vHeight = y;
      vMix = smoothstep(-5.0, 5.0, p.x * 0.6 + broad * 5.0 + sin(t * 0.15 + p.z * 0.2) * 3.0);
      vec4 mv = modelViewMatrix * vec4(p, 1.0);
      float crest = smoothstep(0.2, 1.6, y) + ring * 1.5;
      gl_PointSize = uPixel * (2.0 + crest * 2.6) * (13.0 / -mv.z);
      vFade = smoothstep(26.0, 9.0, -mv.z) * smoothstep(0.8, 3.0, -mv.z) * (0.6 + crest * 0.9);
      gl_Position = projectionMatrix * mv;
    }`,
  fragmentShader: /* glsl */ `
    uniform vec3 uCyan, uRed;
    varying float vHeight, vFade, vMix;
    void main() {
      float d = length(gl_PointCoord - 0.5);
      float a = smoothstep(0.5, 0.05, d);
      vec3 col = mix(uCyan, uRed, vMix);
      col = mix(col, vec3(1.0), smoothstep(0.9, 2.2, vHeight) * 0.7);
      gl_FragColor = vec4(col, a * vFade);
    }`,
});
scene.add(new THREE.Points(geometry, material));

function resize() {
  renderer.setSize(innerWidth, innerHeight, false);
  camera.aspect = innerWidth / innerHeight;
  camera.updateProjectionMatrix();
}
addEventListener('resize', resize);
resize();

const mouse = { x: 0, y: 0 }, look = { x: 0, y: 0 };
addEventListener('pointermove', (e) => { mouse.x = e.clientX / innerWidth - 0.5; mouse.y = e.clientY / innerHeight - 0.5; });

let energyTarget = 1;
window.fx = {
  energy(v) { energyTarget = v; },
  ripple() { uniforms.uRipple.value = 0; if (still) render(0); },
};

let last = performance.now();
function render(dt) {
  uniforms.uEnergy.value += (energyTarget - uniforms.uEnergy.value) * Math.min(1, dt * 3);
  uniforms.uTime.value += dt * (0.55 + 0.45 * uniforms.uEnergy.value);
  uniforms.uRipple.value += dt;
  look.x += (mouse.x - look.x) * Math.min(1, dt * 2.5);
  look.y += (mouse.y - look.y) * Math.min(1, dt * 2.5);
  camera.position.set(look.x * 2.2, 2.1 - look.y * 0.8, 7.5);
  camera.lookAt(look.x * 0.6, 0.6, -4);
  renderer.render(scene, camera);
}
function loop(now) {
  render(Math.min(0.05, (now - last) / 1000));
  last = now;
  requestAnimationFrame(loop);
}
if (still) render(0); else requestAnimationFrame(loop);
canvas.classList.add('ready');
