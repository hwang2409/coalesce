// coalesce live viewer: shows the latest committed revision and follows /events.
import * as THREE from './vendor/three-0.186.1/three.module.js';
import { OrbitControls } from './vendor/three-0.186.1/OrbitControls.js';

const BACKGROUND = 0xe4e8eb;
const EDGE = 0x101820;
const VIEWS = {
  iso: [1, -1, 0.8],
  front: [0, -1, 0],
  right: [1, 0, 0],
  top: [0, -1e-4, 1], // a hair off vertical keeps Z-up orbiting stable and +Y up on screen
};
const $ = (id) => document.getElementById(id);

// ---- renderer, camera, controls

const canvas = $('view');
// preserveDrawingBuffer lets the smoke test (and screenshots) read what was drawn; the stencil
// buffer finds where the section plane is inside a part
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true, stencil: true });
renderer.setPixelRatio(window.devicePixelRatio);
renderer.setClearColor(BACKGROUND);
renderer.autoClear = false;

const scene = new THREE.Scene();
const camera = new THREE.OrthographicCamera(-1, 1, 1, -1, 0.01, 10000);
camera.up.set(0, 0, 1);
camera.position.set(...VIEWS.iso); // a valid orientation before the first scene arrives
const keyLight = new THREE.DirectionalLight(0xffffff, 1.5);
keyLight.position.set(-0.5, 0.6, 1);
camera.add(keyLight);
scene.add(camera, new THREE.HemisphereLight(0xffffff, 0x7d868f, 2.6));

const controls = new OrbitControls(camera, canvas);
controls.enableDamping = false;
controls.addEventListener('change', () => { markUserMoved(); requestRender(); });

const model = new THREE.Group();
const overlap = new THREE.Group();
const diagnostic = new THREE.Group();
const helpers = new THREE.Group();
diagnostic.renderOrder = 20;
scene.add(helpers, model, overlap, diagnostic);

// corner axes, drawn into their own small viewport
const gizmoScene = new THREE.Scene();
const gizmoCamera = new THREE.OrthographicCamera(-1.6, 1.6, 1.6, -1.6, 0.1, 10);
gizmoCamera.up.set(0, 0, 1);
const AXES = [[1, 0, 0, 0xc0392b, 'gx'], [0, 1, 0, 0x2e8b57, 'gy'], [0, 0, 1, 0x2e5f9e, 'gz']];
for (const [x, y, z, color] of AXES) {
  const geometry = new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(x, y, z)]);
  gizmoScene.add(new THREE.Line(geometry, new THREE.LineBasicMaterial({ color })));
}

let frame = 0;
let frames = 0; // completed renders, for the browser test
let shownTop = null; // canvas page top and height at the last render, for the browser test
let shownHeight = 0;
function requestRender() {
  if (!frame) frame = requestAnimationFrame(render);
}

function render() {
  frame = 0;
  const { clientWidth: w, clientHeight: h } = canvas;
  if (!w || !h) return;
  shownTop = canvas.getBoundingClientRect().top;
  shownHeight = h;
  const ratio = renderer.getPixelRatio();
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) renderer.setSize(w, h, false);
  const aspect = w / h;
  const half = (camera.top - camera.bottom) / 2;
  camera.left = -half * aspect;
  camera.right = half * aspect;
  camera.updateProjectionMatrix();

  renderer.setViewport(0, 0, w, h);
  renderer.setScissorTest(false);
  renderer.clear();
  renderer.render(scene, camera);
  updateScale(h);
  frames += 1;
  if ($('gizmo').hidden) return;

  // corner axes follow the main camera's orientation
  const size = 88;
  const direction = camera.position.clone().sub(controls.target).normalize();
  gizmoCamera.position.copy(direction.multiplyScalar(4));
  gizmoCamera.up.copy(camera.up);
  gizmoCamera.lookAt(0, 0, 0);
  gizmoCamera.updateMatrixWorld();
  renderer.setViewport(16, 16, size, size);
  renderer.setScissor(16, 16, size, size);
  renderer.setScissorTest(true);
  renderer.clearDepth();
  renderer.render(gizmoScene, gizmoCamera);
  for (const [x, y, z, , id] of AXES) {
    const p = new THREE.Vector3(x * 1.3, y * 1.3, z * 1.3).project(gizmoCamera);
    const label = $(id);
    label.style.left = `${((p.x + 1) / 2) * size}px`;
    label.style.top = `${((1 - p.y) / 2) * size}px`;
    // an axis pointing at the viewer collapses to a dot; fade its label instead of stacking it
    label.style.opacity = String(Math.min(1, Math.hypot(p.x, p.y) * 1.6));
  }
}

new ResizeObserver(requestRender).observe(canvas);

// ---- scale bar: a round length of about 100 px at the current zoom (48 px on narrow screens)

const narrow = matchMedia('(max-width: 720px)');
const OWNS_ESCAPE = 'input, select, textarea, [contenteditable]:not([contenteditable="false"]), dialog';
$('camera-menu').open = !narrow.matches;
narrow.addEventListener('change', () => { $('camera-menu').open = !narrow.matches; });
const scalePx = () => (narrow.matches ? 48 : 100);

function niceLength(target) {
  const power = 10 ** Math.floor(Math.log10(target));
  const step = [1, 2, 5, 10].find((m) => m * power >= target) ?? 10;
  return step * power;
}

function updateScale(height) {
  $('scale').hidden = !model.children.length;
  if (!model.children.length) return;
  const pxPerMm = (height * camera.zoom) / (camera.top - camera.bottom);
  const mm = niceLength(scalePx() / pxPerMm);
  $('scale-bar').style.width = `${mm * pxPerMm}px`;
  $('scale-text').textContent = `${formatMm(mm)} mm`;
  $('scale-grid').textContent = `, grid ${formatMm(gridStep)} mm`;
}

function formatMm(value) {
  return Number(value.toPrecision(6)).toString();
}

const formatSize = (part) => part.size.map((v) => formatMm(Number(v.toFixed(2)))).join(' × ');

// assembly evidence: three decimals, and a nonzero value never reads as 0
function formatAssemblyMm(value) {
  const rounded = Number(value.toFixed(3)) + 0;
  return rounded === 0 && value !== 0 ? '< 0.001' : String(rounded);
}

// named holes and the features that built the part, so the human can check what the agent made
function partDetail(part) {
  const holes = part.features.filter((feature) => feature.holes).map(({ holes }) => {
    const count = `${holes.length} ${holes.length === 1 ? 'hole' : 'holes'}`;
    const names = holes.filter(Boolean);
    return names.length ? `${count}: ${names.join(', ')}` : count;
  });
  const chain = part.features.map((feature) => feature.id).join(' › ');
  const instance = part.occurrenceId ? `Instance of ${part.definitionLabel} · ${part.partId}` : null;
  return [instance, ...holes, chain && `Built by ${chain}`].filter(Boolean).join('. ');
}

// the canvas's accessible description: what is drawn and how the camera frames it
function describeView() {
  const parts = current?.parts ?? [];
  const framing = activeView ? `${activeView} view, fitted to the model` : 'camera moved by hand';
  const change = current?.change ? ` Latest change: ${current.change.summary}.` : '';
  const at = mode === 'section' ? ` at ${section.axis.toUpperCase()} = ${formatMm(Number(section.position.toFixed(1)))} mm` : '';
  const shown = MODE_TEXT[mode] ? ` Shown ${MODE_TEXT[mode]}${at}; the model is unchanged.` : '';
  const showing = selected ? ` Showing ${selected.partIds.map(partTitle).join(' and ')}, other parts faded.` : '';
  const rejected = diagnostic.children.length ? ' Rejected sketch — not accepted; shown as a dashed red overlay.' : '';
  $('view-description').textContent = parts.length
    ? `Revision ${current.revision}, ${framing}.${change}${shown}${showing}${rejected} `
      + partTitles(parts).map((title, i) => [`${title}, ${formatSize(parts[i])} mm`, partDetail(parts[i])].filter(Boolean).join(', ')).join('; ') + '.'
    : 'No parts to show.';
}

// ---- views

let gridStep = 10;

const surfaceOf = (group) => {
  group.updateWorldMatrix(true, true);
  return group.children[0];
};

function visibleBox() {
  const box = new THREE.Box3();
  // a part's surface, as placed by its group: a section's cut face quad is larger than the part
  for (const child of model.children) if (child.visible) box.expandByObject(surfaceOf(child));
  if (diagnostic.children.length) box.expandByObject(diagnostic);
  if (box.isEmpty() && current) {
    box.min.fromArray(current.bbox.min);
    box.max.fromArray(current.bbox.max);
  }
  return box;
}

function focusBox() {
  const box = new THREE.Box3();
  for (const partId of selected?.partIds ?? []) {
    const part = current.parts.find((item) => (item.occurrenceId ?? item.partId) === partId);
    const group = part && model.getObjectByName(part.name);
    if (group?.visible) box.expandByObject(surfaceOf(group));
  }
  if (overlap.children.length) box.expandByObject(overlap);
  return box;
}

function fitBox(box, name) {
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1);
  const direction = name ? new THREE.Vector3(...VIEWS[name]) : camera.position.clone().sub(controls.target);
  direction.normalize();
  camera.position.copy(center).addScaledVector(direction, radius * 6);
  camera.near = radius * 0.01;
  camera.far = radius * 20;
  // a portrait canvas is narrower than tall: fit the width, not just the height
  const half = radius * 1.15 * Math.max(1, canvas.clientHeight / (canvas.clientWidth || 1));
  camera.top = half;
  camera.bottom = -half;
  camera.zoom = 1;
  controls.target.copy(center);
  controls.update();
  camera.updateProjectionMatrix();
  for (const button of document.querySelectorAll('[data-view]')) {
    button.setAttribute('aria-pressed', String(button.dataset.view === name));
  }
  activeView = name;
  // the grid square is the scale bar's length right after a fit: one scale for both
  gridStep = niceLength(scalePx() / ((canvas.clientHeight || 1) / (camera.top - camera.bottom)));
  rebuildHelpers(current.bbox);
  userMoved = false;
  showRefit(false);
  describeView();
  requestRender();
}

// fits the visible parts; a null name keeps the current view direction
function setView(name) {
  fitBox(visibleBox(), name);
}

let activeView = 'iso'; // null after an orbit, pan or zoom
let userMoved = false; // the camera moved by hand since the last fit: new revisions keep it
let settingView = false;
function markUserMoved() {
  if (settingView) return;
  userMoved = true;
  if (!activeView) return;
  activeView = null;
  for (const button of document.querySelectorAll('[data-view]')) button.setAttribute('aria-pressed', 'false');
  describeView();
}

function applyView(name) {
  settingView = true;
  setView(name);
  settingView = false;
}

// a new revision arrived while the human held their own view: offer the fit, do not force it
function showRefit(on) {
  $('fit').toggleAttribute('data-new', on);
  $('fit-text').textContent = on ? 'Fit new model' : 'Fit';
}

function closeCameraMenu() {
  if (!narrow.matches) return;
  $('camera-menu').open = false;
  $('camera-menu').querySelector('summary').focus();
}

for (const button of document.querySelectorAll('[data-view]')) {
  button.addEventListener('click', () => {
    applyView(button.dataset.view);
    closeCameraMenu();
  });
}
$('fit').addEventListener('click', () => {
  applyView(activeView);
  closeCameraMenu();
});
$('camera-menu').addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || event.defaultPrevented || !narrow.matches || !$('camera-menu').open
      || event.target.closest?.(OWNS_ESCAPE)) return;
  event.preventDefault();
  closeCameraMenu();
});

// off before the first scene and once the link expired: nothing current to aim at
function enableCamera(on) {
  controls.enabled = on;
  for (const control of document.querySelectorAll('.views button, .review button, .review input')) control.disabled = !on;
}
enableCamera(false);

// ---- gpu resources: each group owns its materials; the mesh cache owns part geometry

function clearGroup(group, { ownsGeometry }) {
  group.traverse((object) => {
    object.material?.dispose();
    if (ownsGeometry) object.geometry?.dispose();
  });
  group.clear();
}

// frees every cached part geometry that the shown revision does not use
function pruneGeometries() {
  const used = new Set([
    ...(current?.parts.map((part) => part.mesh.id) ?? []),
    ...(current?.assemblyInspection?.flatMap((row) => row.overlapMesh?.id ?? []) ?? []),
  ]);
  for (const [id, geometry] of geometries) {
    if (used.has(id)) continue;
    geometry.surface.dispose();
    geometry.edges.dispose();
    geometries.delete(id);
  }
}

// ---- ground grid, sized to the model; its center lines mark the origin

function rebuildHelpers(bbox) {
  clearGroup(helpers, { ownsGeometry: true });
  const extent = Math.max(...bbox.max.map((v) => Math.abs(v)), ...bbox.min.map((v) => Math.abs(v)), 1);
  // shortcut: a model far from the origin for its size gets a grid that stops short of it;
  // center the grid on the model if that case turns up.
  const cells = Math.min(Math.ceil((extent * 1.6) / gridStep) * 2, 2000);
  const grid = new THREE.GridHelper(cells * gridStep, cells, 0x9aa3ab, 0xc9cfd4);
  grid.rotateX(Math.PI / 2); // GridHelper lies in XZ; CAD ground is XY with Z up
  grid.material.transparent = true;
  grid.material.opacity = 0.9;
  grid.material.depthWrite = false;
  grid.renderOrder = -1;
  // just under the lowest geometry, so faces resting on z = 0 do not flicker against it
  grid.position.z = Math.min(0, bbox.min[2]) - extent * 0.002;
  helpers.add(grid);
}

// ---- parts

const geometries = new Map(); // mesh id -> {surface, edges}; parts borrow these
const hiddenByUser = new Map(); // part name -> visible override

// the server answers {"superseded": true} for a revision or mesh it dropped for a newer one
class Superseded extends Error {}

async function loadMesh(mesh) {
  if (geometries.has(mesh.id)) return;
  const response = await fetch(mesh.url);
  if (!response.ok) throw new Error(`mesh ${mesh.id}: HTTP ${response.status}`);
  if (response.headers.get('Content-Type') === 'application/json') throw new Superseded(); // meshes are binary
  const buffer = await response.arrayBuffer();
  const v = mesh.vertexCount * 3;
  const t = mesh.triangleCount * 3;
  const surface = new THREE.BufferGeometry();
  surface.setAttribute('position', new THREE.BufferAttribute(new Float32Array(buffer, 0, v), 3));
  surface.setIndex(new THREE.BufferAttribute(new Uint32Array(buffer, v * 4, t), 1));
  surface.computeVertexNormals();
  const edges = new THREE.BufferGeometry();
  edges.setAttribute('position', new THREE.BufferAttribute(new Float32Array(buffer, (v + t) * 4, mesh.edgeVertexCount * 3), 3));
  geometries.set(mesh.id, { surface, edges });
}

// coincident parts (a graph fork leaves bodies with the same box) get rising depth priority, so
// their shared faces show the earlier part instead of z-fighting stripes; every other part keeps
// the default offset and is never pushed behind a part it lies in front of
const MAX_DEPTH_RANK = 3; // shortcut: more coincident copies share the last rank, rare once forks are rejected

function partMatrix(part) {
  const transform = part.transform;
  if (!transform) return new THREE.Matrix4();
  const axis = new THREE.Vector3(...transform.rotation.axis);
  const quaternion = new THREE.Quaternion().setFromAxisAngle(axis, THREE.MathUtils.degToRad(transform.rotation.angleDegrees));
  return new THREE.Matrix4().compose(new THREE.Vector3(...transform.translation), quaternion, new THREE.Vector3(1, 1, 1));
}

function depthRanks(parts) {
  const boxes = parts.map((part) => {
    const { surface } = geometries.get(part.mesh.id);
    if (!surface.boundingBox) surface.computeBoundingBox();
    return surface.boundingBox.clone().applyMatrix4(partMatrix(part));
  });
  const same = (a, b) => a.min.distanceTo(b.min) < 1e-3 && a.max.distanceTo(b.max) < 1e-3;
  return boxes.map((box, i) => Math.min(MAX_DEPTH_RANK, boxes.slice(0, i).filter((other) => same(box, other)).length));
}

function partObject(part, { surface, edges }, rank) {
  const group = new THREE.Group();
  group.name = part.name;
  group.add(new THREE.Mesh(surface, new THREE.MeshStandardMaterial({
    color: part.color,
    roughness: 0.85,
    metalness: 0,
    polygonOffset: true,
    polygonOffsetFactor: 1 + rank,
    polygonOffsetUnits: 1 + rank * 16, // a step of 4 units was not enough in SwiftShader
  })));
  group.add(new THREE.LineSegments(edges, new THREE.LineBasicMaterial({ color: EDGE })));
  group.applyMatrix4(partMatrix(part));
  group.visible = hiddenByUser.get(part.name) ?? part.visible;
  return group;
}

// a part's title: its label, plus the feature that ends it when another part has the same label, plus
// "piece n" by sorted part id (the order that picks split shades) when titles still collide
function partTitles(parts) {
  const group = (keys) => {
    const groups = new Map();
    parts.forEach((part, i) => groups.set(keys[i], [...(groups.get(keys[i]) ?? []), part.occurrenceId ?? part.partId]));
    return groups;
  };
  const labels = group(parts.map((part) => part.label));
  const titles = parts.map((part) => {
    const last = part.features.at(-1)?.id;
    return labels.get(part.label).length > 1 && last && last !== part.label ? `${part.label} \u00b7 ${last}` : part.label;
  });
  const same = group(titles);
  return titles.map((title, i) => {
    const ids = same.get(title).toSorted();
    return ids.length > 1 ? `${title}, piece ${ids.indexOf(parts[i].occurrenceId ?? parts[i].partId) + 1}` : title;
  });
}

// narrow screens show a part's detail line only while its row has focus, and its facts from when its
// row gets focus until another part's do, so nothing below them jumps when focus moves on. wide
// screens always show both. say so to screen readers.
let openPart = null;
function syncExpanded(button) {
  const facts = button.classList.contains('has-facts');
  const open = facts ? button.dataset.partId === openPart : document.activeElement === button;
  button.classList.toggle('open', facts && open);
  if (narrow.matches) button.setAttribute('aria-expanded', String(open));
  else button.removeAttribute('aria-expanded');
}
const syncAllExpanded = () => document.querySelectorAll('.part.has-detail, .part.has-facts').forEach(syncExpanded);
narrow.addEventListener('change', syncAllExpanded);

function partFacts(part) {
  const facts = document.createElement('span');
  facts.className = 'part-facts';
  for (const { label, value } of part.facts) {
    const fact = document.createElement('span');
    fact.className = 'fact';
    const name = document.createElement('span');
    name.className = 'fact-label';
    name.textContent = label;
    const text = document.createElement('span');
    text.className = 'fact-value';
    text.textContent = value;
    fact.append(name, text);
    facts.append(fact);
  }
  return facts;
}

function renderPartList(parts) {
  const list = $('parts');
  list.replaceChildren();
  if (!parts.length) {
    const empty = document.createElement('li');
    empty.className = 'parts-empty';
    empty.textContent = 'This revision has no parts';
    list.append(empty);
    return;
  }
  const titles = partTitles(parts);
  parts.forEach((part, index) => {
    const object = model.getObjectByName(part.name);
    const item = document.createElement('li');
    // the swatch is the show/hide control; the row itself only shows details
    const toggle = document.createElement('button');
    toggle.type = 'button';
    toggle.className = 'visibility';
    toggle.setAttribute('aria-pressed', String(object.visible));
    toggle.setAttribute('aria-label', `Show ${titles[index]}`);
    toggle.title = `Show or hide ${titles[index]}`;
    toggle.dataset.title = titles[index];
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = part.color;
    toggle.append(swatch);
    // the user's choice; the shown state also follows the review mode
    toggle.addEventListener('click', () => {
      const shown = !(hiddenByUser.get(part.name) ?? part.visible);
      hiddenByUser.set(part.name, shown);
      toggle.setAttribute('aria-pressed', String(shown));
      applyMode();
      requestRender();
    });
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'part';
    button.dataset.partId = part.occurrenceId ?? part.partId;
    button.setAttribute('aria-label', titles[index]);
    const name = document.createElement('span');
    name.className = 'part-name';
    name.textContent = titles[index];
    name.classList.toggle('collides', titles[index] !== part.label);
    name.title = part.name;
    const title = document.createElement('span');
    title.className = 'part-title';
    title.append(name);
    const size = document.createElement('span');
    size.className = 'part-size';
    size.textContent = formatSize(part);
    button.append(title, size);
    const solve = current?.assemblyState?.occurrences.find((row) => row.occurrenceId === part.occurrenceId);
    if (solve) {
      const placement = solve.grounded ? 'grounded' : solve.mateIds.length ? 'mated' : 'explicit';
      const state = document.createElement('span');
      state.className = `occurrence-state ${placement}`;
      state.textContent = `${placement} · ${solve.remainingDof} DOF`;
      title.append(state);
    }
    if (part.role === 'reference') {
      const role = document.createElement('span');
      role.className = 'part-role';
      role.textContent = 'Reference, not exported';
      button.append(role);
    }
    const detail = partDetail(part);
    if (detail) {
      const line = document.createElement('span');
      line.className = 'part-detail';
      line.textContent = detail;
      button.append(line);
      button.classList.add('has-detail');
    }
    if (part.facts.length) {
      button.append(partFacts(part));
      button.classList.add('has-facts');
    }
    if (detail || part.facts.length) {
      const hint = document.createElement('span');
      hint.className = 'disclosure';
      hint.setAttribute('aria-hidden', 'true');
      hint.textContent = '\u25BE';
      title.append(hint);
      button.addEventListener('focus', () => {
        if (part.facts.length) openPart = part.occurrenceId ?? part.partId;
        syncAllExpanded();
      });
      button.addEventListener('blur', () => syncExpanded(button));
      syncExpanded(button);
    }
    button.addEventListener('click', () => button.focus()); // safari does not focus a clicked button; focus shows the detail line
    item.append(toggle, button);
    list.append(item);
  });
}

// ---- assembly solve: persisted mate intent and the effective occurrence freedom

function remainingMotionSummary(state) {
  const free = state.occurrences.filter((row) => row.remainingDof > 0);
  const motionName = (motion) => ({
    axisSpin: 'spin', normalSpin: 'spin', axisTranslation: 'axial translation',
    inPlaneX: 'in-plane translation', inPlaneY: 'in-plane translation',
    globalRigidMotion: 'global rigid motion',
  })[motion] ?? motion;
  const groups = new Map();
  for (const row of free) {
    const part = current.parts.find((item) => item.occurrenceId === row.occurrenceId);
    const label = (part?.partId?.split('/')[0] ?? part?.label ?? row.occurrenceId).toLowerCase();
    const motions = [...new Set(row.freeMotions.map(motionName))].join(', ');
    const key = `${label}\0${motions}`;
    groups.set(key, { label, motions, count: (groups.get(key)?.count ?? 0) + 1 });
  }
  return {
    free,
    text: [...groups.values()].map(({ label, motions, count }) =>
      `${count > 1 ? `${count} ${label}s` : label}: ${motions}`).join(' · '),
    unexpected: free.some((row) => row.freeMotions.some((motion) => motion !== 'axisSpin')),
  };
}

function renderAssemblyState(state) {
  const section = $('assembly-state');
  section.hidden = !state;
  if (!state) return;
  const motion = remainingMotionSummary(state);
  $('solve-status').textContent = state.conflicts.length
    ? 'solve conflict'
    : motion.free.length ? 'solved · free motion remains' : 'solved · fully constrained';
  const warning = $('solve-warning');
  warning.hidden = !motion.text && !state.conflicts.length;
  warning.classList.toggle('unexpected', state.conflicts.length > 0 || motion.unexpected);
  warning.textContent = state.conflicts.length
    ? `${state.conflicts.length} solve conflict${state.conflicts.length === 1 ? '' : 's'}`
    : motion.text;
  const grounded = state.occurrences.filter((row) => row.grounded).length;
  $('solve-summary').textContent = `${grounded} grounded · ${state.conflicts.length} conflicts`;
  const inspection = groupAssemblyRows(current?.assemblyInspection ?? []);
  const passed = inspection.filter((row) => row.status === 'PASS').length;
  const inspectionSummary = $('inspection-summary');
  inspectionSummary.hidden = !inspection.length;
  inspectionSummary.classList.toggle('failure', inspection.some((row) => row.status !== 'PASS'));
  inspectionSummary.textContent = inspection.length
    ? `all ${inspection.length} pairs checked · ${passed === inspection.length ? 'pass' : `${inspection.length - passed} need attention`}`
    : '';
  const bom = state.bom ?? { rows: [], excluded: [] };
  $('bom-count').textContent = `${bom.rows.length} ${bom.rows.length === 1 ? 'definition' : 'definitions'}`;
  const bomList = $('bom-rows');
  bomList.replaceChildren();
  for (const row of bom.rows) {
    const item = document.createElement('li');
    item.className = 'bom-row';
    item.textContent = `${row.label} · ${row.count}`;
    bomList.append(item);
  }
  $('bom-excluded').textContent = bom.excluded.length
    ? `${bom.excluded.length} reference ${bom.excluded.length === 1 ? 'part' : 'parts'} excluded`
    : '';
  $('mate-count').textContent = `${state.mates.length}`;
  const list = $('mate-rows');
  list.replaceChildren();
  $('mate-rows').closest('details').open = !narrow.matches;
  for (const mate of state.mates) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'mate-row';
    button.dataset.mateId = mate.mateId;
    const key = `mate/${mate.mateId}`;
    button.setAttribute('aria-pressed', String(selected?.key === key));
    const heading = document.createElement('span');
    heading.className = 'mate-heading';
    heading.textContent = `${mate.mateId} · ${mate.kind}`;
    const relation = document.createElement('span');
    relation.className = 'mate-relation';
    relation.textContent = mate.occurrenceIds.join(' ↔ ');
    const detail = document.createElement('span');
    detail.className = 'mate-detail';
    const setting = mate.offset ? `${mate.offset.value} ${mate.offset.unit}` : mate.orientation;
    detail.textContent = [mate.status, setting].filter(Boolean).join(' · ');
    button.append(heading, relation, detail);
    button.addEventListener('click', () => {
      if (selected?.key === key) showAll();
      else showPart({ key, partIds: mate.occurrenceIds });
    });
    item.append(button);
    list.append(item);
  }
}

// ---- fit check: the enclosure inspection of this revision, one row per measurement

// problems first: on a phone only the first rows show without scrolling. a measured value has no
// threshold, so it is no gate result: its badge says "measured only", never anything like a pass
const STATUS_TEXT = { ERROR: 'ERROR', FAIL: 'FAIL', UNKNOWN: 'UNKNOWN', PASS: 'PASS', MEASURED: 'MEASURED ONLY' };
const severity = (row) => ['ERROR', 'FAIL', 'UNKNOWN'].indexOf(row.status) >>> 0; // not a problem: last, in order
const partRow = (partId) => partId && document.querySelector(`.part[data-part-id="${CSS.escape(partId)}"]`);
const partTitle = (partId) => partRow(partId)?.querySelector('.part-name').textContent ?? null;

// the fit row whose part the canvas shows, the other parts faded; the camera stays where it is
let selected = null; // {key, partIds}
let passingExpanded = false;

function fitRow(row, key) {
  const part = partRow(row.partId);
  const item = document.createElement(part ? 'button' : 'div');
  item.className = 'fit-row';
  item.dataset.status = row.status;
  const cells = [
    ['fit-status', STATUS_TEXT[row.status] ?? row.status],
    ['fit-label', row.label],
    // a value with no requirement keeps its note on the same line: only a problem gets its own line
    ['fit-value', row.status === 'MEASURED' ? `${row.value} · ${row.reason}` : row.value],
    ['fit-reason', row.status === 'MEASURED' ? null : row.reason],
  ];
  for (const [className, text] of cells) {
    if (!text) continue;
    const cell = document.createElement('span');
    cell.className = className;
    cell.textContent = text;
    item.append(cell);
  }
  if (part) {
    const title = part.querySelector('.part-name').textContent;
    const shown = selected?.key === key;
    item.type = 'button';
    item.setAttribute('aria-pressed', String(shown));
    item.title = shown ? 'Show all parts again' : `Show ${title} in the view and its facts`;
    item.dataset.partId = row.partId;
    // a second press shows every part again
    item.addEventListener('click', () => {
      if (shown) {
        showAll();
        return;
      }
      showPart({ key, partIds: [row.partId] });
      part.focus({ preventScroll: true });
      if (!narrow.matches) part.scrollIntoView({ block: 'nearest' });
    });
  }
  item.dataset.key = key;
  return item;
}

const fitRowFor = (key) => document.querySelector(`.fit-row[data-key="${CSS.escape(key)}"]`);

function showPart(next) {
  selected = next;
  renderAssemblyState(current.assemblyState);
  renderInspection(current.fit, current.assemblyInspection);
  applyMode();
  if (selected?.overlapMesh) fitBox(focusBox(), activeView);
  describeView();
  requestRender();
}

// every part again, and the canvas says so. focus goes back to the fit row that showed the part, else
// to the part's row, else to the parts heading: the row it was on may be gone in a new revision
function showAll(focus = true) {
  const { key, partIds, overlapMesh } = selected;
  showPart(null);
  if (overlapMesh) applyView(activeView);
  const row = fitRowFor(key);
  if (focus) (row || partRow(partIds[0]) || $('parts-title')).focus();
  $('view-description').textContent = `All parts shown. ${$('view-description').textContent}`;
}

// the canvas says which part it shows: after a press, focus and the panel move on to that part's row
$('showing-clear').addEventListener('click', () => showAll());
// Escape shows all parts again; in the section controls their own Escape ends the section view first.
// a field or a dialog keeps its own Escape: defaultPrevented does not cover browser defaults
document.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape' || event.defaultPrevented || !selected || event.target.closest?.(OWNS_ESCAPE)) return;
  event.preventDefault();
  showAll();
});

// the measurements in words, with the part each was measured on; the raw record keeps the ids
function evidenceLine(row) {
  const on = partTitle(row.partId);
  const what = on ? `${row.label} on ${on}` : row.label;
  return `${what}: ${row.value}, ${(STATUS_TEXT[row.status] ?? row.status).toLowerCase()}`;
}

function pointEvidence(row) {
  if (row.id !== 'screw_closure' || !row.evidence?.points) return [];
  return row.evidence.points.toSorted((a, b) => severity(a) - severity(b)).map((point) => {
    const wall = point.residualWall?.value;
    const measured = wall === undefined ? 'wall not measured' : `wall ${formatMm(wall)} mm`;
    const axis = point.coaxial === null ? 'axis not measured' : point.coaxial ? 'coaxial' : 'not coaxial';
    const line = document.createElement('li');
    line.dataset.point = point.name;
    line.dataset.status = point.status;
    line.textContent = `${point.name}: ${measured}, ${axis} — ${point.status.toLowerCase()}`;
    return line;
  });
}

function assemblyKey(revision, row) {
  return `assembly/${JSON.stringify([revision, row.occurrenceIds.toSorted()])}`;
}

function groupAssemblyRows(rows) {
  const groups = new Map();
  for (const row of rows.toSorted((a, b) => a.requirementId.localeCompare(b.requirementId, undefined, { numeric: true }))) {
    const key = JSON.stringify(row.occurrenceIds.toSorted());
    const current = groups.get(key);
    const requirementIds = [...(current?.requirementIds ?? []), row.requirementId]
      .toSorted((a, b) => a.localeCompare(b, undefined, { numeric: true }));
    const evidence = !current || severity(row) < severity(current) ? row : current;
    groups.set(key, { ...evidence, requirementIds });
  }
  return [...groups.values()];
}

function assemblyValue(row) {
  const overlap = row.overlapVolume?.value;
  const clearance = row.clearance?.value;
  if (row.error) return row.error.message;
  if (overlap > 0) return `${formatAssemblyMm(overlap)} mm³ overlap`;
  if (clearance === undefined) return 'No overlap';
  if (clearance <= current.assemblyLinearTolerance) return 'touching';
  return `${formatAssemblyMm(clearance)} mm clearance`;
}

function assemblyRow(row, key) {
  const item = document.createElement('button');
  item.type = 'button';
  item.className = 'fit-row assembly-row';
  item.dataset.status = row.status;
  item.dataset.key = key;
  const titles = row.occurrenceIds.map(partTitle);
  const value = assemblyValue(row);
  for (const [className, text] of [
    ['fit-status', row.status],
    ['fit-label', titles.join(' ↔ ')],
    ['fit-value', value],
    ['fit-reason', row.requirementIds.join(' · ')],
  ]) {
    const cell = document.createElement('span');
    cell.className = className;
    cell.textContent = text;
    item.append(cell);
  }
  const shown = selected?.key === key;
  item.setAttribute('aria-pressed', String(shown));
  item.setAttribute('aria-label', `${shown ? 'Show all parts again' : `Focus ${titles.join(' and ')}`}: ${row.status}, ${value}`);
  item.addEventListener('click', () => {
    if (shown) return showAll();
    showPart({ ...row, key, partIds: row.occurrenceIds });
    fitRowFor(key)?.focus();
  });
  return item;
}

function orderedAssemblyRows(rows) {
  const natural = new Intl.Collator(undefined, { numeric: true }).compare;
  const rank = (row) => row.status === 'ERROR' ? 0 : row.status === 'FAIL' ? 1 : 2;
  const clearance = (row) => row.clearance?.value <= current.assemblyLinearTolerance
    ? 0 : Number((row.clearance?.value ?? Infinity).toFixed(3));
  return rows.toSorted((a, b) => rank(a) - rank(b)
    || (a.status === 'PASS' ? clearance(a) - clearance(b) : 0)
    || natural(a.occurrenceIds.join('/'), b.occurrenceIds.join('/')));
}

function renderAssemblyList(rows, list, evidence) {
  const ordered = orderedAssemblyRows(rows);
  const passing = ordered.filter((row) => row.status === 'PASS');
  const extraPassing = passing.slice(6);
  const shown = passingExpanded
    ? ordered
    : ordered.filter((row) => row.status !== 'PASS' || passing.indexOf(row) < 6
      || assemblyKey(current.revision, row) === selected?.key);
  for (const row of shown) {
    const item = document.createElement('li');
    item.append(assemblyRow(row, assemblyKey(current.revision, row)));
    list.append(item);
  }
  if (extraPassing.length) {
    const item = document.createElement('li');
    item.className = 'assembly-more';
    const button = document.createElement('button');
    const affected = extraPassing.filter((row) => assemblyKey(current.revision, row) !== selected?.key);
    button.type = 'button';
    button.setAttribute('aria-expanded', String(passingExpanded));
    button.textContent = passingExpanded
      ? `Hide ${affected.length} passing pairs`
      : `Show ${affected.length} more passing pairs`;
    button.addEventListener('click', () => {
      passingExpanded = !passingExpanded;
      renderInspection(current.fit, current.assemblyInspection);
      document.querySelector('.assembly-more button')?.focus();
    });
    item.append(button);
    list.append(item);
  }
  for (const row of ordered) {
    const line = document.createElement('li');
    line.textContent = `${row.occurrenceIds.map(partTitle).join(' and ')}: ${row.status.toLowerCase()} — ${row.requirementIds.join(' · ')}`;
    evidence.append(line);
  }
}

function renderInspectionCount(rows) {
  const counts = Object.entries(STATUS_TEXT)
    .map(([status, text]) => [rows.filter((row) => row.status === status).length, text.toLowerCase()])
    .filter(([count]) => count)
    .map(([count, text]) => Object.assign(document.createElement('span'), { textContent: `${count} ${text}` }));
  $('inspection-count').replaceChildren(...counts.flatMap((count, i) => (i ? [', ', count] : [count])));
  $('inspection-note').hidden = !rows.some((row) => row.status === 'MEASURED');
}

function renderFitList(fit, list, evidence) {
  for (const summary of fit) {
    if (fit.length > 1) {
      const heading = document.createElement('li');
      heading.className = 'fit-base';
      heading.textContent = summary.baseId;
      list.append(heading);
      evidence.append(heading.cloneNode(true));
    }
    for (const row of summary.rows.toSorted((a, b) => severity(a) - severity(b))) {
      const item = document.createElement('li');
      item.append(fitRow(row, `${summary.baseId}/${row.id}`));
      list.append(item);
      const line = document.createElement('li');
      line.textContent = evidenceLine(row);
      evidence.append(line, ...pointEvidence(row));
    }
  }
}

function selectionEvidenceText(row) {
  if (!row?.status) return '';
  if (row.status === 'ERROR') return 'Inspection error';
  if (row.status === 'UNKNOWN') return 'Inspection unknown';
  if (row.overlapVolume?.value > 0) return `Exact overlap: ${formatAssemblyMm(row.overlapVolume.value)} mm³`;
  if (row.clearance?.value <= current.assemblyLinearTolerance) return 'Touching · no collision';
  return row.status === 'PASS' ? 'Pass · no overlap' : row.status;
}

function renderSelectionEvidence() {
  $('showing').hidden = !selected;
  $('showing-text').textContent = selected ? `Showing ${selected.partIds.map(partTitle).join(' and ')}, other parts faded.` : '';
  const badge = $('selection-evidence');
  badge.hidden = !selected?.status;
  badge.dataset.status = selected?.status ?? '';
  badge.textContent = selectionEvidenceText(selected);
}

function renderInspection(fit, assembly = []) {
  $('inspection').hidden = !fit.length && !assembly.length;
  $('inspection-name').textContent = fit.length ? 'Fit check' : 'Assembly inspection';
  const assemblyRows = groupAssemblyRows(assembly);
  if (selected?.key.startsWith('assembly/')) {
    const refreshed = assemblyRows.find((row) => assemblyKey(current.revision, row) === selected.key);
    if (refreshed) selected = { ...refreshed, key: selected.key, partIds: refreshed.occurrenceIds };
  }
  renderInspectionCount([...fit.flatMap((summary) => summary.rows), ...assemblyRows]);
  const list = $('inspection-rows');
  const evidence = $('inspection-evidence');
  list.replaceChildren();
  evidence.replaceChildren();
  renderFitList(fit, list, evidence);
  renderAssemblyList(assemblyRows, list, evidence);
  $('inspection-raw').textContent = JSON.stringify({
    fit: fit.map(({ baseId, rows }) => ({
      baseId,
      rows: rows.map(({ id, status, evidence: data }) => ({ id, status, evidence: data })),
    })),
    assembly,
  }, null, 1);
  renderSelectionEvidence();
  updateMore();
}

// ---- panel: a cue below the scrolled panel content while more lies below it

const panel = $('panel-scroll');
function updateMore() {
  const bottom = panel.getBoundingClientRect().bottom;
  const below = panel.scrollTop + panel.clientHeight < panel.scrollHeight - 4;
  const checks = !$('inspection').hidden && $('inspection').getBoundingClientRect().bottom > bottom;
  $('more').hidden = !below;
  $('more').textContent = checks ? 'Scroll for more results and parts' : 'Scroll for more parts';
}
panel.addEventListener('scroll', updateMore, { passive: true });
// what a disclosure opens would otherwise start under the panel's bottom edge
for (const details of document.querySelectorAll('.fit-evidence, .fit-raw')) {
  details.addEventListener('toggle', () => details.open && details.scrollIntoView({ block: 'nearest' }));
}
const panelSize = new ResizeObserver(updateMore);
for (const element of [panel, ...panel.querySelectorAll(':scope > section')]) panelSize.observe(element);

// ---- review modes: lid off, exploded and one section plane. they only move, hide or cut what this
// page draws; the scene data and the document never change, and Assembled puts every part back.

renderer.localClippingEnabled = true;
let mode = 'assembled';
const section = { axis: 'y', position: null }; // null: the middle of the model
const sectionPlane = new THREE.Plane();
// keep the half away from the iso camera, so the cut faces the human (also from Front, Right and Top)
const SECTION_NORMALS = { x: [-1, 0, 0], y: [0, 1, 0], z: [0, 0, -1] };
const MODE_NAMES = { assembled: 'Assembled', 'lid-off': 'Lid off', exploded: 'Exploded', section: 'Section' };
const MODE_TEXT = { 'lid-off': 'lid off', exploded: 'exploded along Z', section: 'section view' };

const isLid = (part) => part.features[0]?.kind === 'pcb lid';
// exploded layers, bottom to top: the box and other made parts, the board reference, the lid
const layer = (part) => (isLid(part) ? 2 : part.role === 'reference' ? 1 : 0);

function unavailable(name, parts) {
  if (name !== 'assembled' && !parts.length) return 'No parts to show';
  if (name === 'lid-off' && !parts.some(isLid)) return 'No lid in this model';
  if (name === 'exploded' && new Set(parts.map(layer)).size < 2) return 'No lid or board to separate';
  return null;
}

// a part's assembled z range, from its cached geometry (computed by depthRanks)
const zRange = (part) => {
  const { min, max } = geometries.get(part.mesh.id).surface.boundingBox;
  return [min.z, max.z];
};

// each layer is lifted so it clears the layer below by one fixed gap: 0.4 of the model's height (at least 1 mm)
function explodeOffsets(parts) {
  const gap = Math.max(0.4 * (current.bbox.max[2] - current.bbox.min[2]), 1);
  const offsets = new Map();
  let top = null;
  for (const level of [0, 1, 2]) {
    const members = parts.filter((part) => layer(part) === level);
    if (!members.length) continue;
    const ranges = members.map(zRange);
    const low = Math.min(...ranges.map((r) => r[0]));
    const lift = top === null ? 0 : Math.max(0, top + gap - low);
    for (const part of members) offsets.set(part.name, lift);
    top = Math.max(...ranges.map((r) => r[1])) + lift;
  }
  return offsets;
}

// the slider's range along the section axis. each end stops one 0.1 mm step past the outer wall of an
// enclosure (the base's wall and floor, the lid's top), so the plane at either end still opens the inside
// instead of showing a slab of that wall. any other part keeps one step inside its outer face: a full cross-section.
const isBase = (part) => part.features[0]?.kind === 'pcb base';
const factMm = (part, label) => part?.facts.find((fact) => fact.label === label)?.mm ?? 0;
function sectionRange(parts) {
  const i = 'xyz'.indexOf(section.axis);
  // shortcut: one wall for every lid, from the first base; give each lid its base's wall if a model has two
  const wall = factMm(parts.find(isBase), 'Wall');
  const insets = (part) => {
    if (!isBase(part) && !isLid(part)) return [0, 0];
    if (i < 2) return [wall, wall]; // the lid covers the base's outer outline
    return isLid(part) ? [0, factMm(part, 'Thickness')] : [factMm(part, 'Floor'), 0];
  };
  const ends = parts.map((part) => {
    const box = part.bbox
      ? new THREE.Box3(new THREE.Vector3(...part.bbox.min), new THREE.Vector3(...part.bbox.max))
      : geometries.get(part.mesh.id).surface.boundingBox;
    const [low, high] = insets(part);
    return [box.min.getComponent(i) + low, box.max.getComponent(i) - high];
  });
  const low = Number((Math.min(...ends.map((end) => end[0])) + 0.1).toFixed(1));
  const high = Number((Math.max(...ends.map((end) => end[1])) - 0.1).toFixed(1));
  const middle = Number(((low + high) / 2).toFixed(1));
  return low <= high ? [low, high] : [middle, middle]; // a part thinner than its walls and two steps
}

// every part is cut, the board too, so nothing uncut hides what the cut opens up. each cut part counts,
// per pixel, its back faces minus its front faces behind the plane in the stencil buffer: non-zero means
// the plane is inside the part there, and a light quad on the plane draws the cut face only there
function setCut(group, part, index, on) {
  const [surface, lines, ...cap] = group.children;
  if (Boolean(cap.length) === on) return;
  for (const object of [surface, lines]) {
    // a fresh material: disposing the old one also frees the shader it compiled for this cut state
    const material = object.material.clone();
    material.clippingPlanes = on ? [sectionPlane] : null;
    object.material.dispose();
    object.material = material;
  }
  if (!on) {
    for (const object of cap) {
      object.material.dispose();
      if (object.geometry !== surface.geometry) object.geometry.dispose(); // the surface belongs to the mesh cache
      group.remove(object);
    }
    return;
  }
  const counting = { colorWrite: false, depthWrite: false, depthTest: false, stencilWrite: true, clippingPlanes: [sectionPlane] };
  for (const [side, op] of [[THREE.BackSide, THREE.IncrementWrapStencilOp], [THREE.FrontSide, THREE.DecrementWrapStencilOp]]) {
    const counter = new THREE.Mesh(surface.geometry, new THREE.MeshBasicMaterial({
      ...counting, side, stencilFail: op, stencilZFail: op, stencilZPass: op,
    }));
    counter.renderOrder = 2 * index + 1; // after every part's surface, before this part's cut face
    group.add(counter);
  }
  // the quad also sets the stencil back to 0, so the next part counts from a clean buffer
  const box = geometries.get(part.mesh.id).surface.boundingBox;
  const face = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({
    color: new THREE.Color(part.color).lerp(new THREE.Color(0xffffff), 0.45),
    side: THREE.DoubleSide,
    stencilWrite: true,
    stencilRef: 0,
    stencilFunc: THREE.NotEqualStencilFunc,
    stencilFail: THREE.ReplaceStencilOp,
    stencilZFail: THREE.ReplaceStencilOp,
    stencilZPass: THREE.ReplaceStencilOp,
  }));
  face.scale.setScalar(box.min.distanceTo(box.max) * 1.1); // covers the part's whole cut at any position
  face.renderOrder = 2 * index + 2;
  group.add(face);
}

// a faded part stays in place as a faint outline, so the selected part reads in context. its cut face, if
// any, stays opaque: the stencil count that draws cut faces must run in draw order with the solid parts
function fade(group, on) {
  const [surface, lines] = group.children;
  if (surface.material.transparent === on) return;
  for (const [object, opacity] of [[surface, 0.12], [lines, 0.25]]) {
    Object.assign(object.material, { transparent: on, opacity: on ? opacity : 1, depthWrite: !on, needsUpdate: true });
  }
}

const INSPECTION_COLORS = { FAIL: 0xc62828, ERROR: 0xc77800, UNKNOWN: 0xc77800, PASS: 0x2f7a46 };
const OVERLAP_PAIR_COLORS = [0xc62828, 0x2367c9];

function showOverlap() {
  clearGroup(overlap, { ownsGeometry: false });
  const mesh = selected?.status === 'FAIL' && selected.overlapVolume?.value > 0
    ? selected.overlapMesh : null;
  if (!mesh) return;
  const geometry = geometries.get(mesh.id);
  const surface = new THREE.Mesh(geometry.surface, new THREE.MeshBasicMaterial({
    color: 0xffc400, transparent: true, opacity: 0.92, depthTest: false, depthWrite: false,
  }));
  const edges = new THREE.LineSegments(geometry.edges, new THREE.LineBasicMaterial({
    color: 0x4a2100, depthTest: false, depthWrite: false,
  }));
  surface.name = 'exact-overlap';
  edges.name = 'exact-overlap-edges';
  surface.renderOrder = edges.renderOrder = 10;
  overlap.add(surface, edges);
}

// the enclosure-only review modes apply only when the scene has an enclosure fit check
function hasEnclosure() {
  return Boolean(current?.fit?.length);
}

// places, shows and cuts every drawn part for the current mode; a mode that stops applying ends
function setSection(parts) {
  const [low, high] = sectionRange(parts);
  section.position = Math.min(Math.max(section.position ?? (low + high) / 2, low), high);
  const normal = new THREE.Vector3(...SECTION_NORMALS[section.axis]);
  // drawn a hair past the shown position so a face exactly on the plane does not stipple
  const nudge = 1e-4 * Math.max(high - low, 1);
  sectionPlane.set(normal, -(section.position + nudge) * normal.getComponent('xyz'.indexOf(section.axis)));
  Object.assign($('section-range'), { min: low, max: high });
  $('section-range').value = section.position;
  Object.assign($('section-value'), { min: low, max: high });
  if (document.activeElement !== $('section-value')) $('section-value').value = Number(section.position.toFixed(1));
}

function positionCutFace(group, part) {
  const face = group.children[4];
  const bounds = part.bbox
    ? new THREE.Box3(new THREE.Vector3(...part.bbox.min), new THREE.Vector3(...part.bbox.max))
    : geometries.get(part.mesh.id).surface.boundingBox.clone().applyMatrix4(group.matrixWorld);
  const position = sectionPlane.projectPoint(bounds.getCenter(new THREE.Vector3()), new THREE.Vector3());
  group.worldToLocal(position);
  face.position.copy(position);
  const worldFace = new THREE.Quaternion().setFromUnitVectors(new THREE.Vector3(0, 0, 1), sectionPlane.normal);
  face.quaternion.copy(group.getWorldQuaternion(new THREE.Quaternion()).invert().multiply(worldFace));
}

function applyPartMode(part, index, offsets, toggle) {
  const group = model.getObjectByName(part.name);
  group.position.set(...(part.transform?.translation ?? [0, 0, 0]));
  group.position.z += offsets?.get(part.name) ?? 0;
  const lifted = mode === 'lid-off' && isLid(part);
  group.visible = (hiddenByUser.get(part.name) ?? part.visible) && !lifted;
  setCut(group, part, index, mode === 'section');
  const identity = part.occurrenceId ?? part.partId;
  const focused = selected?.partIds.includes(identity);
  const pairIndex = selected?.overlapMesh ? selected.partIds.indexOf(identity) : -1;
  const color = pairIndex >= 0 ? OVERLAP_PAIR_COLORS[pairIndex] : INSPECTION_COLORS[selected?.status];
  group.children[0].material.color.set(focused && color ? color : part.color);
  group.children[1].material.color.set(pairIndex >= 0 ? color : EDGE);
  fade(group, Boolean(selected) && !focused);
  if (pairIndex >= 0) Object.assign(group.children[0].material, {
    transparent: true, opacity: 0.32, depthWrite: false, needsUpdate: true,
  });
  if (mode === 'section') positionCutFace(group, part);
  toggle.disabled = lifted;
  toggle.title = lifted ? 'Hidden by the Lid off view' : `Show or hide ${toggle.dataset.title}`;
}

function renderModeControls(parts, enclosure) {
  const buttons = [...document.querySelectorAll('[data-mode]')];
  const focusSection = !enclosure && buttons.some((button) => button === document.activeElement && button.dataset.mode !== 'section');
  for (const button of buttons) {
    const reason = unavailable(button.dataset.mode, parts);
    button.hidden = !enclosure && button.dataset.mode !== 'section';
    button.setAttribute('aria-pressed', String(button.dataset.mode === mode));
    if (reason) {
      button.setAttribute('aria-disabled', 'true');
      button.title = reason;
    } else {
      button.removeAttribute('aria-disabled');
      button.removeAttribute('title');
    }
  }
  if (focusSection) document.querySelector('[data-mode="section"]').focus();
}

// places, shows and cuts every drawn part for the current mode; a mode that stops applying ends
function applyMode() {
  const parts = current?.parts ?? [];
  const enclosure = hasEnclosure();
  if ((!enclosure && mode !== 'section') || unavailable(mode, parts)) mode = 'assembled';
  const offsets = mode === 'exploded' ? explodeOffsets(parts) : null;
  if (mode === 'section') setSection(parts);
  const toggles = document.querySelectorAll('#parts .visibility');
  parts.forEach((part, index) => applyPartMode(part, index, offsets, toggles[index]));
  showOverlap();
  renderModeControls(parts, enclosure);
  $('modes').classList.toggle('single', !enclosure);
  $('section-help').textContent = enclosure ? 'Escape returns to the Assembled view.' : 'Escape returns to the default view.';
  for (const button of document.querySelectorAll('[data-axis]')) {
    button.setAttribute('aria-pressed', String(button.dataset.axis === section.axis));
  }
  $('section-controls').hidden = mode !== 'section';
}


function hint(text) {
  $('review-hint').hidden = !text;
  $('review-hint').textContent = text ?? '';
}

function setMode(name) {
  $('review-announce').textContent = '';
  const reason = unavailable(name, current?.parts ?? []);
  if (reason) {
    hint(`${MODE_NAMES[name]}: ${reason.charAt(0).toLowerCase()}${reason.slice(1)}`);
    return;
  }
  hint(null);
  mode = name;
  refresh();
}

// redraws after a mode or section change; a camera the human has not moved refits to what is shown
function refresh() {
  applyMode();
  if (current?.parts.length && !userMoved) applyView(activeView);
  describeView();
  requestRender();
}

for (const button of document.querySelectorAll('[data-mode]')) {
  button.addEventListener('click', () => setMode(button.dataset.mode));
  // a mode that does not apply says why when it gets focus, and stops saying it when focus leaves
  button.addEventListener('focus', () => {
    const reason = unavailable(button.dataset.mode, current?.parts ?? []);
    if (reason) setMode(button.dataset.mode);
  });
  button.addEventListener('blur', () => hint(null));
}
for (const button of document.querySelectorAll('[data-axis]')) {
  button.addEventListener('click', () => {
    section.axis = button.dataset.axis;
    section.position = null;
    refresh();
  });
}
for (const input of [$('section-range'), $('section-value')]) {
  input.addEventListener('input', () => {
    if (input.value === '' || Number.isNaN(Number(input.value))) return; // a number being typed
    section.position = Number(input.value);
    applyMode();
    describeView();
    requestRender();
  });
}
// Escape in the section controls ends the section view; the hint says so while they have focus
const sectionControls = $('section-controls');
sectionControls.addEventListener('focusin', () => hint(`Esc returns to ${hasEnclosure() ? 'Assembled' : 'the default view'}`));
sectionControls.addEventListener('focusout', (event) => {
  if (!sectionControls.contains(event.relatedTarget)) hint(null);
});
sectionControls.addEventListener('keydown', (event) => {
  if (event.key !== 'Escape') return;
  event.preventDefault();
  const enclosed = hasEnclosure();
  setMode('assembled');
  document.querySelector(`[data-mode="${enclosed ? 'assembled' : 'section'}"]`).focus(); // the hidden controls drop focus
  $('review-announce').textContent = `Section view ended; ${enclosed ? 'Assembled' : 'default'} view restored.`;
});
$('section-value').addEventListener('change', () => {
  $('section-value').value = Number(section.position.toFixed(1)); // shows the clamped position
});

// ---- state shown to the human

let current = null; // last scene drawn
let wanted = 0; // newest revision announced
let failure = null;

function notice(title, body = '') {
  $('notice').hidden = !title;
  $('notice-title').textContent = title ?? '';
  $('notice-body').textContent = body;
}

const ENDED = 'Viewer ended';
const ENDED_HELP = 'This page cannot reconnect. If coalesce runs again, open the new link it printed '
  + 'to its error output (stderr), on a line like this:';
const ENDED_LINE = 'coalesce viewer: http://127.0.0.1:<port>/#token=<key>';
const ENDED_ASK = 'Or ask your agent to call query with select server.';
const ENDED_COPY = 'The coalesce viewer I had open has ended. Call the coalesce query tool with '
  + '{"select": {"kind": "server"}} and give me the viewerUrl it returns.';

// the ended page's one action: a message to paste into the agent chat
function copyButton() {
  const button = document.createElement('button');
  button.type = 'button';
  button.className = 'copy';
  button.textContent = 'Copy request for your agent';
  button.addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(ENDED_COPY);
      button.textContent = 'Copied. Paste it to your agent';
    } catch {
      button.textContent = 'Could not copy. Ask your agent to call query with select server';
    }
  });
  return button;
}

// states: connecting, live, lost (retrying for a bounded time), ended (terminal), denied
function setLink(state, text) {
  $('link').dataset.state = state;
  $('link-text').textContent = text;
  if (state === 'ended') enableCamera(false);
  showStatus();
  syncGizmo();
}

// one status at a time: while the link is down that is the news, and a failed edit is one sentence of it
function showStatus() {
  const state = $('link').dataset.state;
  const stale = (state === 'lost' || state === 'ended') && current !== null;
  const line = $('stale');
  line.hidden = !stale;
  line.dataset.state = state;
  const ids = failure?.featureIds.length ? `, ${failure.featureIds.join(', ')},` : '';
  const failed = failure ? ` The last edit${ids} failed.` : '';
  if (state === 'ended' && stale) {
    const title = document.createElement('strong');
    title.textContent = `${ENDED}. Revision ${current.revision} is the last one this page saw.`;
    const sample = document.createElement('code');
    sample.textContent = ENDED_LINE;
    line.replaceChildren(title, `${failed} ${ENDED_HELP} `, sample, ` ${ENDED_ASK} `, copyButton());
  } else {
    line.textContent = stale ? `Revision ${current.revision} may be out of date. Reconnecting to coalesce.${failed}` : '';
  }
  $('failure').hidden = !failure || stale;
  $('stage').toggleAttribute('data-stale', stale);
}

// orientation means nothing without a model, or once the page can no longer follow it
function syncGizmo() {
  const state = $('link').dataset.state;
  $('gizmo').hidden = !model.children.length || state === 'ended' || state === 'denied';
  requestRender();
}

function formatQuantity({ value, unit }) {
  return `${value} ${unit}`;
}

function showRepairHistory(repairs) {
  const section = $('repair-history');
  section.hidden = !repairs.length;
  $('repair-history-rows').replaceChildren(...repairs.flatMap((repair) =>
    Object.entries(repair.fields).map(([name, values]) => {
      const row = document.createElement('li');
      const feature = document.createElement('code');
      feature.textContent = repair.featureId;
      const field = document.createElement('span');
      field.className = 'repair-field';
      field.textContent = name;
      const change = document.createElement('span');
      change.className = 'repair-values';
      change.textContent = `Failed ${formatQuantity(values.attempted)} → accepted ${formatQuantity(values.accepted)}`;
      row.append(feature, field, change);
      return row;
    })
  ));
}

function showRevision(sceneData) {
  showRepairHistory(sceneData.repairHistory ?? []);
  const label = $('revision');
  label.textContent = String(sceneData.revision);
  delete label.dataset.empty;
  label.classList.add('fresh');
  requestAnimationFrame(() => requestAnimationFrame(() => label.classList.remove('fresh')));
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const count = sceneData.parts.length;
  $('revision-meta').textContent = `${count} ${count === 1 ? 'part' : 'parts'}, shown at ${time}`;
  // what the agent's commit did, apart from the number; none for a document opened from disk
  $('change').hidden = !sceneData.change;
  $('change').textContent = sceneData.change ? capitalize(sceneData.change.summary) : '';
  $('change').title = sceneData.change ? capitalize(sceneData.change.summary) : '';
}

// what to do next, in words; the stable code and raw message stay under "Error details"
const VERIFIED_REPAIR_CODES = new Set([
  'UNIT_MISMATCH', 'INVALID_PARAMETER', 'REVISION_CONFLICT', 'UNKNOWN_ID',
  'DUPLICATE_ID', 'CYCLE', 'TARGET_ALREADY_CONSUMED',
]);
function repair({ code, message, hint, redundantConstraints, conflictingConstraints }, feature) {
  if (hint) return `${capitalize(message)}. Ask your agent to ${hint}.`;
  if (code === 'SKETCH_REDUNDANT' && redundantConstraints?.length) {
    return `Sketch ${feature} rejected: redundant constraints ${redundantConstraints.join(', ')}`;
  }
  if (code === 'SKETCH_CONFLICTING' && conflictingConstraints?.length) {
    return `Sketch ${feature} rejected: conflicting constraints ${conflictingConstraints.join(', ')}`;
  }
  if (!VERIFIED_REPAIR_CODES.has(code)) {
    return `${feature} failed: ${message}. The last good model is still shown.`;
  }
  if (code === 'UNIT_MISMATCH') return `A value has the wrong unit (${message}). Ask your agent to send it in the expected unit.`;
  if (code === 'INVALID_PARAMETER') return `A value in the edit is not valid: ${message}. Ask your agent to correct it.`;
  if (code === 'REVISION_CONFLICT') return 'The edit was based on an older revision. Ask your agent to read the current revision and try again.';
  if (code === 'UNKNOWN_ID') return `The edit names something that does not exist: ${message}. Ask your agent to check the id.`;
  if (code === 'DUPLICATE_ID') return `An id in the edit is already in use: ${message}. Ask your agent to pick a new id.`;
  if (code === 'CYCLE') return 'The edit makes a feature depend on itself. Ask your agent to reorder the features.';
  return 'The edit targets an older point in a body lineage. Ask your agent to use the reported legal target.';
}
const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1);

function clearDiagnostic() {
  clearGroup(diagnostic, { ownsGeometry: true });
  delete diagnostic.userData.frame;
  $('rejected-label').hidden = true;
  $('diagnostic-omitted').hidden = true;
  describeView();
  requestRender();
}

function diagnosticPoints(entity) {
  if (entity.kind === 'point') return [[entity.x, entity.y]];
  if (entity.kind === 'line') return [entity.start, entity.end];
  const segments = entity.kind === 'circle' ? 64 : 48;
  let start = entity.kind === 'circle' ? 0 : entity.startAngle;
  let end = entity.kind === 'circle' ? Math.PI * 2 : entity.endAngle;
  while (end <= start) end += Math.PI * 2;
  return Array.from({ length: segments + 1 }, (_, index) => {
    const angle = start + (end - start) * index / segments;
    return [entity.center[0] + Math.cos(angle) * entity.radius,
      entity.center[1] + Math.sin(angle) * entity.radius];
  });
}

function diagnosticPoint([x, y], frame) {
  return new THREE.Vector3(...frame.origin)
    .addScaledVector(new THREE.Vector3(...frame.xAxis), x)
    .addScaledVector(new THREE.Vector3(...frame.yAxis), y);
}

function showDiagnostic(geometry) {
  clearDiagnostic();
  if (!geometry || geometry.accepted !== false) return;
  diagnostic.userData.frame = geometry.frame;
  for (const [id, entity] of Object.entries(geometry.entities)) {
    const points = diagnosticPoints(entity).map((point) => diagnosticPoint(point, geometry.frame));
    const shape = new THREE.BufferGeometry().setFromPoints(points);
    let object;
    if (entity.kind === 'point') {
      object = new THREE.Points(shape, new THREE.PointsMaterial({ color: 0xb42318, size: 8, sizeAttenuation: false, depthTest: false }));
    } else {
      object = new THREE.Line(shape, new THREE.LineDashedMaterial({ color: 0xb42318, dashSize: 1.2, gapSize: 0.7, depthTest: false }));
      object.computeLineDistances();
    }
    object.name = id;
    object.userData.kind = entity.kind;
    object.renderOrder = 20;
    diagnostic.add(object);
  }
  $('rejected-label').hidden = false;
  if (diagnostic.children.length) applyView(activeView);
  describeView();
  requestRender();
}

function showFailure() {
  showStatus();
  clearDiagnostic();
  if (!failure) return;
  $('failure-title').textContent = failure.revision == null
    ? 'Last edit failed. There is no good revision yet.'
    : `Last edit failed. Showing revision ${failure.revision}, the last good model.`;
  const ids = $('failure-features');
  ids.replaceChildren(...failure.featureIds.map((id) => {
    const chip = document.createElement('code');
    chip.className = 'feature-id';
    chip.textContent = id;
    return chip;
  }));
  ids.hidden = !failure.featureIds.length;
  $('failure-repair').textContent = repair(failure.error, failure.featureIds[0] || 'Edit');
  $('failure-code').textContent = failure.error.code;
  $('failure-message').textContent = failure.error.message;
  $('failure-kernel').hidden = !failure.error.kernelMessage;
  $('failure-kernel').textContent = failure.error.kernelMessage ? `Kernel: ${failure.error.kernelMessage}` : '';
  showDiagnostic(failure.diagnosticGeometry);
  $('diagnostic-omitted').hidden = !failure.diagnosticGeometryOmitted;
}

// loads run one at a time, then catch up to the newest announced revision,
// so the mesh cache is never pruned under an in-flight load.
let loading = false;
async function loadRevision(revision, refresh = false) {
  wanted = Math.max(wanted, revision);
  if (loading) return;
  loading = true;
  try {
    let tried = null;
    while ((refresh || wanted !== current?.revision) && wanted !== tried) {
      tried = wanted;
      refresh = false;
      await loadOne(tried);
    }
  } finally {
    loading = false;
  }
}

// all unique meshes at once; allSettled so none is still in flight when this returns
async function loadMeshes(parts) {
  const unique = new Map(parts.map((part) => [part.mesh.id, part.mesh]));
  const results = await Promise.allSettled([...unique.values()].map(loadMesh));
  const failed = results.find((result) => result.status === 'rejected');
  if (failed) throw failed.reason;
}

// the shown part still exists and its fit row still points at it
function selectionExists(data, { partIds, key }) {
  const identities = new Set(data.parts.map((part) => part.occurrenceId ?? part.partId));
  if (!partIds.every((partId) => identities.has(partId))) return false;
  if (key.startsWith('assembly/')) return data.assemblyInspection?.some((row) => assemblyKey(data.revision, row) === key);
  if (key.startsWith('mate/')) return data.assemblyState?.mates.some((mate) => `mate/${mate.mateId}` === key);
  return data.fit.some(({ baseId, rows }) => rows.some((row) => `${baseId}/${row.id}` === key && partIds.includes(row.partId)));
}

function showScene(data, dropped, held) {
  for (const group of model.children) setCut(group, null, 0, false); // frees each cut face's own quad
  clearGroup(model, { ownsGeometry: false });
  const ranks = depthRanks(data.parts);
  data.parts.forEach((part, index) => model.add(partObject(part, geometries.get(part.mesh.id), ranks[index])));
  current = data;
  renderPartList(data.parts);
  renderAssemblyState(data.assemblyState);
  renderInspection(data.fit, data.assemblyInspection);
  applyMode(); // the chosen review mode stays while it applies to the new revision
  showRevision(data);
  const empty = !data.parts.length;
  notice(empty ? `Revision ${data.revision} has no solid parts yet` : null,
    'Sketches are not drawn here. Parts appear when a feature makes a solid.');
  enableCamera(!empty);
  if (empty) clearGroup(helpers, { ownsGeometry: true });
  else if (userMoved) {
    rebuildHelpers(data.bbox);
    showRefit(true);
  } else applyView(activeView);
  syncGizmo();
  if (dropped) showAll(held);
  else {
    if (held) (fitRowFor(selected.key) || partRow(selected.partIds[0]))?.focus();
    describeView();
  }
  requestRender();
}

async function loadOne(revision) {
  if (!current) notice(`Loading revision ${revision}`);
  else $('revision-meta').textContent = `Loading revision ${revision}`;
  try {
    const response = await fetch(`/api/scene/${revision}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.superseded) throw new Superseded();
    await loadMeshes([
      ...data.parts,
      ...(data.assemblyInspection ?? []).filter((row) => row.overlapMesh)
        .map((row) => ({ mesh: row.overlapMesh })),
    ]);
    if (revision !== wanted) return; // a newer revision arrived meanwhile
    // a shown part outlives a new revision while its fit row still points at it; else all parts again,
    // and focus on its row follows to the replacement row
    const dropped = selected && !selectionExists(data, selected);
    const held = selected && [fitRowFor(selected.key), partRow(selected.partIds[0])].includes(document.activeElement);
    showScene(data, dropped, held);
  } catch (error) {
    // superseded: the event for the newer revision is already here or on its way
    if (error instanceof Superseded || revision !== wanted) return;
    if (!current) notice(`Could not load revision ${revision}`, String(error.message));
    else $('revision-meta').textContent = `Could not load revision ${revision}: ${error.message}`;
  } finally {
    pruneGeometries(); // also drops meshes a stale or failed load fetched
  }
}

// ---- connection

async function connect() {
  const token = new URLSearchParams(location.hash.slice(1)).get('token');
  // the token lives only in the fragment; trade it for an HttpOnly cookie, then drop it.
  const access = token ? await session({ method: 'POST', body: JSON.stringify({ token }) }) : await session();
  if (token) history.replaceState(null, '', location.pathname);
  if (access !== true) {
    setLink('denied', 'No access');
    document.querySelector('.revision-line').hidden = true;
    document.querySelector('.parts').hidden = true;
    $('inspection').hidden = true;
    $('revision-meta').textContent = 'Open the link printed as “coalesce viewer:” to see the current model';
    notice('Open the viewer link that coalesce printed',
      'This page needs the access key in that link, after #token=. coalesce prints the link to its '
      + 'error output (stderr): your terminal, or your agent\'s MCP server log.');
    return;
  }

  listen();
}

// whether our cookie (or the posted token) gives access; null while the server is unreachable
async function session(init = {}) {
  try {
    const response = await fetch('/api/session', { cache: 'no-store', headers: { 'Content-Type': 'application/json' }, ...init });
    return (await response.json()).authorized === true;
  } catch {
    return null;
  }
}

function listen() {
  const events = new EventSource('/events');
  events.addEventListener('open', () => {
    clearDiagnostic();
    setLink('live', 'Live');
    if (lostAt !== null && current) {
      const meta = $('revision-meta');
      meta.textContent = `reconnected · revision ${current.revision}`;
      meta.classList.add('reconnected');
      setTimeout(() => meta.classList.remove('reconnected'), 2600);
      lostAt = null;
    }
    if (!current) notice('Waiting for the first revision',
      'Geometry appears here after your agent commits its first edit. This page updates by itself.');
  });
  events.addEventListener('error', () => {
    // reconnect only after the probe, so a stream never retries with a key the server dropped
    events.close();
    lost();
  });
  events.addEventListener('revision', (event) => {
    const { revision, refresh = false } = JSON.parse(event.data);
    if (failure && revision > failure.revision) {
      failure = null;
      clearDiagnostic();
      showFailure();
    }
    loadRevision(revision, refresh);
  });
  events.addEventListener('failure', (event) => {
    failure = JSON.parse(event.data);
    showFailure();
  });
}

// a restarted coalesce has a new key and, almost always, a new port: this page cannot follow it.
// retry a brief drop for a bounded time, then say the viewer ended and stop.
const OUTAGE_LIMIT_MS = 15000;
let lostAt = null;
async function lost() {
  lostAt ??= Date.now();
  setLink('lost', 'Reconnecting');
  if (!current) notice('Disconnected from coalesce', 'Reconnecting to coalesce.');
  const remaining = Math.max(1, OUTAGE_LIMIT_MS - (Date.now() - lostAt));
  const access = await session({ signal: AbortSignal.timeout(remaining) });
  if (access === null && Date.now() - lostAt < OUTAGE_LIMIT_MS) {
    setTimeout(lost, 2000);
  } else if (access) {
    listen();
  } else {
    setLink('ended', ENDED);
    if (!current) notice(ENDED, `${ENDED_HELP} \u201c${ENDED_LINE}\u201d. ${ENDED_ASK}`);
  }
}

window.coalesceViewer = {
  get revision() { return current?.revision ?? null; },
  get failure() { return failure; },
  get diagnostic() {
    return {
      entityCount: diagnostic.children.length,
      kinds: diagnostic.children.map((child) => child.userData.kind).sort(),
      frame: diagnostic.userData.frame ?? null,
    };
  },
};
if (window.coalesceDebug === true) {
  // read-only gpu counters for the browser stress test; a test sets the flag before load
  Object.defineProperty(window.coalesceViewer, 'memory', {
    get: () => ({
      geometries: renderer.info.memory.geometries,
      textures: renderer.info.memory.textures,
      programs: renderer.info.programs.length,
      cachedMeshes: geometries.size,
    }),
  });
  // world points [x, y, z] in normalized device coordinates, through the camera that drew the frame
  window.coalesceViewer.project = (points) => points.map((p) => new THREE.Vector3(...p).project(camera).toArray().slice(0, 2));
  window.coalesceViewer.overlapScreenBounds = () => {
    if (!overlap.children.length) return null;
    const rect = canvas.getBoundingClientRect();
    const projected = corners(new THREE.Box3().setFromObject(overlap)).map((point) => point.project(camera));
    const xs = projected.map((point) => rect.left + (point.x + 1) * rect.width / 2);
    const ys = projected.map((point) => rect.top + (1 - point.y) * rect.height / 2);
    return {
      x: Math.floor(Math.min(...xs)),
      y: Math.floor(Math.min(...ys)),
      width: Math.ceil(Math.max(...xs)) - Math.floor(Math.min(...xs)),
      height: Math.ceil(Math.max(...ys)) - Math.floor(Math.min(...ys)),
    };
  };
  // scene identity and render state, so the test waits for a finished frame, not a timer
  Object.defineProperty(window.coalesceViewer, 'drawn', {
    get: () => ({
      frames,
      settled: frame === 0 && shownTop === canvas.getBoundingClientRect().top && shownHeight === canvas.clientHeight,
      revision: current?.revision ?? null,
      parts: model.children.map((group) => group.name),
      visible: model.children.map((group) => group.visible),
      meshes: current?.parts.map((part) => part.mesh.id) ?? [],
      geometries: model.children.map((group) => group.children[0].geometry.uuid),
      mode,
      positions: model.children.map((group) => group.position.toArray()),
      worldBounds: Object.fromEntries(model.children.map((group) => {
        const { min, max } = new THREE.Box3().setFromObject(surfaceOf(group));
        return [group.name, { min: min.toArray(), max: max.toArray() }];
      })),
      cut: model.children.map((group) => Boolean(group.children[0].material.clippingPlanes?.length)),
      faded: model.children.map((group) => group.children[0].material.transparent),
      colors: model.children.map((group) => `#${group.children[0].material.color.getHexString()}`),
      opacities: model.children.map((group) => group.children[0].material.opacity),
      overlapCount: overlap.children.length,
      focusProjected: focusBox().isEmpty() ? null
        : corners(focusBox()).map((point) => point.project(camera).toArray().slice(0, 2)),
      edgeStyles: model.children.map((group) => ({
        color: `#${group.children[1].material.color.getHexString()}`,
        opacity: group.children[1].material.opacity,
      })),
      zRanges: model.children.map((group) => {
        const { min, max } = new THREE.Box3().setFromObject(surfaceOf(group));
        return [min.z, max.z];
      }),
      camera: controls.enabled,
      view: [...camera.quaternion.toArray(), camera.zoom], // orientation and zoom, which an orbit sets
      // the visible parts' box corners in normalized device coordinates (-1..1 is on screen)
      projected: visibleBox().isEmpty() ? null : corners(visibleBox()).map((p) => p.project(camera).toArray().slice(0, 2)),
    }),
  });
}

const corners = ({ min, max }) => [0, 1, 2, 3, 4, 5, 6, 7].map((i) => new THREE.Vector3(
  i & 1 ? max.x : min.x, i & 2 ? max.y : min.y, i & 4 ? max.z : min.z));

requestRender();
connect();
