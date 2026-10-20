// coalesce live viewer: shows the latest committed revision and follows /events.
import * as THREE from './vendor/three-0.186.1/three.module.js';
import { OrbitControls } from './vendor/three-0.186.1/OrbitControls.js';

const BACKGROUND = 0xe4e8eb;
const EDGE = 0x1d2329;
const VIEWS = {
  iso: [1, -1, 0.8],
  front: [0, -1, 0],
  right: [1, 0, 0],
  top: [0, -1e-4, 1], // a hair off vertical keeps Z-up orbiting stable and +Y up on screen
};
const $ = (id) => document.getElementById(id);

// ---- renderer, camera, controls

const canvas = $('view');
// preserveDrawingBuffer lets the smoke test (and screenshots) read what was drawn.
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, preserveDrawingBuffer: true });
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
const helpers = new THREE.Group();
scene.add(helpers, model);

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
let shownTop = null; // canvas page top and height at the last render
let shownHeight = 0;
function requestRender() {
  if (!frame) frame = requestAnimationFrame(render);
}

// the status row moves the canvas top edge: keep the model's scale and screen position
function holdModel(top, h) {
  const unitsPerPx = (camera.top - camera.bottom) / camera.zoom / shownHeight;
  const half = ((camera.top - camera.bottom) / 2) * (h / shownHeight);
  camera.top = half;
  camera.bottom = -half;
  const down = top + h / 2 - (shownTop + shownHeight / 2); // css px the view center moved
  const offset = new THREE.Vector3(0, 1, 0).applyQuaternion(camera.quaternion).multiplyScalar(-down * unitsPerPx);
  camera.position.add(offset);
  controls.target.add(offset);
}

function render() {
  frame = 0;
  const { clientWidth: w, clientHeight: h } = canvas;
  if (!w || !h) return;
  const top = canvas.getBoundingClientRect().top;
  if (shownTop !== null && top !== shownTop) holdModel(top, h);
  shownTop = top;
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

// named holes and the features that built the part, so the human can check what the agent made
function partDetail(part) {
  const holes = part.features.filter((feature) => feature.holes).map(({ holes }) => {
    const count = `${holes.length} ${holes.length === 1 ? 'hole' : 'holes'}`;
    const names = holes.filter(Boolean);
    return names.length ? `${count}: ${names.join(', ')}` : count;
  });
  const chain = part.features.map((feature) => feature.id).join(' › ');
  return [...holes, chain && `Built by ${chain}`].filter(Boolean).join('. ');
}

// the canvas's accessible description: what is drawn and how the camera frames it
function describeView() {
  const parts = current?.parts ?? [];
  const framing = activeView ? `${activeView} view, fitted to the model` : 'camera moved by hand';
  const change = current?.change ? ` Latest change: ${current.change.summary}.` : '';
  $('view-description').textContent = parts.length
    ? `Revision ${current.revision}, ${framing}.${change} `
      + partTitles(parts).map((title, i) => [`${title}, ${formatSize(parts[i])} mm`, partDetail(parts[i])].filter(Boolean).join(', ')).join('; ') + '.'
    : 'No parts to show.';
}

// ---- views

let gridStep = 10;

function visibleBox() {
  const box = new THREE.Box3();
  for (const child of model.children) if (child.visible) box.expandByObject(child);
  if (box.isEmpty() && current) {
    box.min.fromArray(current.bbox.min);
    box.max.fromArray(current.bbox.max);
  }
  return box;
}

// fits the visible parts; a null name keeps the current view direction
function setView(name) {
  const box = visibleBox();
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

for (const button of document.querySelectorAll('[data-view]')) {
  button.addEventListener('click', () => applyView(button.dataset.view));
}
$('fit').addEventListener('click', () => applyView(activeView));

// off before the first scene and once the link expired: nothing current to aim at
function enableCamera(on) {
  controls.enabled = on;
  for (const button of document.querySelectorAll('.views button')) button.disabled = !on;
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
  const used = new Set(current?.parts.map((part) => part.mesh.id));
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

function depthRanks(parts) {
  const boxes = parts.map((part) => {
    const { surface } = geometries.get(part.mesh.id);
    if (!surface.boundingBox) surface.computeBoundingBox();
    return surface.boundingBox;
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
  group.visible = hiddenByUser.get(part.name) ?? part.visible;
  return group;
}

// a part's title: its label, plus the feature that ends it when another part has the same label
function partTitles(parts) {
  const counts = new Map();
  for (const part of parts) counts.set(part.label, (counts.get(part.label) ?? 0) + 1);
  return parts.map((part) => {
    const last = part.features.at(-1)?.id;
    return counts.get(part.label) > 1 && last && last !== part.label ? `${part.label} \u00b7 ${last}` : part.label;
  });
}

// narrow screens show a part's detail line only while its row has focus: say so to screen readers
function syncExpanded(button) {
  if (narrow.matches) button.setAttribute('aria-expanded', String(document.activeElement === button));
  else button.removeAttribute('aria-expanded');
}
narrow.addEventListener('change', () => document.querySelectorAll('.part.has-detail').forEach(syncExpanded));

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
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = part.color;
    toggle.append(swatch);
    toggle.addEventListener('click', () => {
      object.visible = !object.visible;
      hiddenByUser.set(part.name, object.visible);
      toggle.setAttribute('aria-pressed', String(object.visible));
      requestRender();
    });
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'part';
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
    const detail = partDetail(part);
    if (detail) {
      const hint = document.createElement('span');
      hint.className = 'disclosure';
      hint.setAttribute('aria-hidden', 'true');
      hint.textContent = '\u25BE';
      title.append(hint);
      const line = document.createElement('span');
      line.className = 'part-detail';
      line.textContent = detail;
      button.append(line);
      button.classList.add('has-detail');
      button.addEventListener('focus', () => syncExpanded(button));
      button.addEventListener('blur', () => syncExpanded(button));
      syncExpanded(button);
    }
    button.addEventListener('click', () => button.focus()); // safari does not focus a clicked button; focus shows the detail line
    item.append(toggle, button);
    list.append(item);
  });
}

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
  const stale = (state === 'lost' || state === 'ended') && current !== null;
  const line = $('stale');
  line.hidden = !stale;
  line.dataset.state = state;
  if (state === 'ended') {
    enableCamera(false);
    const title = document.createElement('strong');
    title.textContent = `${ENDED}. Revision ${current?.revision} is the last one this page saw.`;
    const sample = document.createElement('code');
    sample.textContent = ENDED_LINE;
    line.replaceChildren(title, ` ${ENDED_HELP} `, sample, ` ${ENDED_ASK} `, copyButton());
  } else {
    line.textContent = stale ? `Revision ${current.revision} may be out of date. Reconnecting to coalesce.` : '';
  }
  $('stage').toggleAttribute('data-stale', stale);
  syncGizmo();
}

// orientation means nothing without a model, or once the page can no longer follow it
function syncGizmo() {
  const state = $('link').dataset.state;
  $('gizmo').hidden = !model.children.length || state === 'ended' || state === 'denied';
  requestRender();
}

function showRevision(sceneData) {
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
}

// what to do next, in words; the stable code and raw message stay under "Error details"
const REFERENCE_CODES = new Set(['EMPTY', 'SPLIT', 'MERGED', 'AMBIGUOUS']);
function repair({ code, message, hint }) {
  if (hint) return `${capitalize(message)}. Ask your agent to ${hint}.`;
  if (code === 'UNIT_MISMATCH') return `A value has the wrong unit (${message}). Ask your agent to send it in the expected unit.`;
  if (code === 'INVALID_PARAMETER') return `A value in the edit is not valid: ${message}. Ask your agent to correct it.`;
  if (code === 'REVISION_CONFLICT') return 'The edit was based on an older revision. Ask your agent to read the current revision and try again.';
  if (code === 'UNKNOWN_ID') return `The edit names something that does not exist: ${message}. Ask your agent to check the id.`;
  if (code === 'DUPLICATE_ID') return `An id in the edit is already in use: ${message}. Ask your agent to pick a new id.`;
  if (code === 'CYCLE' || code === 'ORDER') return 'The edit makes a feature depend on itself or on a later feature. Ask your agent to reorder the features.';
  if (REFERENCE_CODES.has(code)) return 'A stored face or edge reference no longer matches exactly one piece of geometry. Ask your agent to select it again.';
  if (code === 'KERNEL_OPERATION_FAILED' || code === 'INVALID_BREP') {
    return 'These values do not make valid geometry. Check that every size and distance is greater than 0 mm and that each feature meets the part it changes.';
  }
  return `${capitalize(message)}.`;
}
const capitalize = (text) => text.charAt(0).toUpperCase() + text.slice(1);

function showFailure() {
  $('failure').hidden = !failure;
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
  $('failure-repair').textContent = repair(failure.error);
  $('failure-code').textContent = failure.error.code;
  $('failure-message').textContent = failure.error.message;
}

// loads run one at a time, then catch up to the newest announced revision,
// so the mesh cache is never pruned under an in-flight load.
let loading = false;
async function loadRevision(revision) {
  wanted = Math.max(wanted, revision);
  if (loading) return;
  loading = true;
  try {
    let tried = null;
    while (wanted !== current?.revision && wanted !== tried) {
      tried = wanted;
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

async function loadOne(revision) {
  if (!current) notice(`Loading revision ${revision}`);
  else $('revision-meta').textContent = `Loading revision ${revision}`;
  try {
    const response = await fetch(`/api/scene/${revision}`, { cache: 'no-store' });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    if (data.superseded) throw new Superseded();
    await loadMeshes(data.parts);
    if (revision !== wanted) return; // a newer revision arrived meanwhile
    clearGroup(model, { ownsGeometry: false });
    const ranks = depthRanks(data.parts);
    data.parts.forEach((part, index) => model.add(partObject(part, geometries.get(part.mesh.id), ranks[index])));
    current = data;
    renderPartList(data.parts);
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
    describeView();
    requestRender();
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
    setLink('live', 'Live');
    if (!current) notice('Waiting for the first revision',
      'Geometry appears here after your agent commits its first edit. This page updates by itself.');
  });
  events.addEventListener('error', () => {
    // reconnect only after the probe, so a stream never retries with a key the server dropped
    events.close();
    lost();
  });
  events.addEventListener('revision', (event) => {
    const { revision } = JSON.parse(event.data);
    failure = null;
    showFailure();
    loadRevision(revision);
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
  const access = await session();
  if (access === null && Date.now() - lostAt < OUTAGE_LIMIT_MS) {
    setTimeout(lost, 2000);
  } else if (access) {
    lostAt = null;
    listen();
  } else {
    setLink('ended', ENDED);
    if (!current) notice(ENDED, `${ENDED_HELP} \u201c${ENDED_LINE}\u201d. ${ENDED_ASK}`);
  }
}

window.coalesceViewer = {
  get revision() { return current?.revision ?? null; },
  get failure() { return failure; },
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
