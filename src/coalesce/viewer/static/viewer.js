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
controls.addEventListener('change', () => { markCustomView(); requestRender(); });

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

// ---- scale bar: a round length of about 100 px at the current zoom

function niceLength(target) {
  const power = 10 ** Math.floor(Math.log10(target));
  const step = [1, 2, 5, 10].find((m) => m * power >= target) ?? 10;
  return step * power;
}

function updateScale(height) {
  if (!current) return;
  const pxPerMm = (height * camera.zoom) / (camera.top - camera.bottom);
  const mm = niceLength(100 / pxPerMm);
  $('scale').hidden = false;
  $('scale-bar').style.width = `${mm * pxPerMm}px`;
  $('scale-text').textContent = `${formatMm(mm)} mm, grid ${formatMm(gridStep)} mm`;
}

function formatMm(value) {
  return Number(value.toPrecision(6)).toString();
}

// ---- views

let fitted = false;
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

function setView(name) {
  const box = visibleBox();
  if (box.isEmpty()) return;
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 1);
  const direction = new THREE.Vector3(...(VIEWS[name] ?? VIEWS.iso)).normalize();
  camera.position.copy(center).addScaledVector(direction, radius * 6);
  camera.near = radius * 0.01;
  camera.far = radius * 20;
  camera.top = radius * 1.15;
  camera.bottom = -radius * 1.15;
  camera.zoom = 1;
  controls.target.copy(center);
  controls.update();
  camera.updateProjectionMatrix();
  for (const button of document.querySelectorAll('[data-view]')) {
    button.setAttribute('aria-pressed', String(button.dataset.view === name));
  }
  activeView = name;
  requestRender();
}

let activeView = 'iso';
let settingView = false;
function markCustomView() {
  if (settingView || !activeView) return;
  activeView = null;
  for (const button of document.querySelectorAll('[data-view]')) button.setAttribute('aria-pressed', 'false');
}

function applyView(name) {
  settingView = true;
  setView(name);
  settingView = false;
}

for (const button of document.querySelectorAll('[data-view]')) {
  button.addEventListener('click', () => applyView(button.dataset.view));
}
$('fit').addEventListener('click', () => applyView(activeView ?? 'iso'));

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
  const extent = Math.max(...bbox.max.map((v, i) => Math.abs(v)), ...bbox.min.map((v) => Math.abs(v)), 1);
  gridStep = niceLength(extent / 6);
  const cells = Math.ceil((extent * 1.6) / gridStep) * 2;
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

// the server answers 204 for a revision or mesh it dropped for a newer one
class Superseded extends Error {}

async function loadMesh(mesh) {
  if (geometries.has(mesh.id)) return;
  const response = await fetch(mesh.url);
  if (response.status === 204) throw new Superseded();
  if (!response.ok) throw new Error(`mesh ${mesh.id}: HTTP ${response.status}`);
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

function partObject(part, { surface, edges }) {
  const group = new THREE.Group();
  group.name = part.name;
  group.add(new THREE.Mesh(surface, new THREE.MeshStandardMaterial({
    color: part.color,
    roughness: 0.85,
    metalness: 0,
    polygonOffset: true,
    polygonOffsetFactor: 1,
    polygonOffsetUnits: 1,
  })));
  group.add(new THREE.LineSegments(edges, new THREE.LineBasicMaterial({ color: EDGE })));
  group.visible = hiddenByUser.get(part.name) ?? part.visible;
  return group;
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
  for (const part of parts) {
    const object = model.getObjectByName(part.name);
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'part';
    button.setAttribute('aria-pressed', String(object.visible));
    button.title = `Show or hide ${part.label}`;
    const swatch = document.createElement('span');
    swatch.className = 'swatch';
    swatch.style.background = part.color;
    const name = document.createElement('span');
    name.className = 'part-name';
    name.textContent = part.label;
    name.title = part.name;
    const size = document.createElement('span');
    size.className = 'part-size';
    size.textContent = part.size.map((v) => formatMm(Number(v.toFixed(2)))).join(' × ');
    button.append(swatch, name, size);
    button.addEventListener('click', () => {
      object.visible = !object.visible;
      hiddenByUser.set(part.name, object.visible);
      button.setAttribute('aria-pressed', String(object.visible));
      requestRender();
    });
    item.append(button);
    list.append(item);
  }
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

const NEW_LINK = 'Open the new viewer link';
const NEW_LINK_HELP = 'coalesce restarted with a new access key and printed the new link to its '
  + 'error output (stderr): your terminal, or your agent\'s MCP server log. '
  + 'The line starts with \u201ccoalesce viewer:\u201d.';

// states: connecting, live, lost (retrying), ended (server has a new key), denied
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
    title.textContent = `Revision ${current?.revision} is stale.`;
    line.replaceChildren(title, ` ${NEW_LINK}. ${NEW_LINK_HELP}`);
  } else {
    line.textContent = stale ? `Revision ${current.revision} may be out of date. This page reconnects by itself.` : '';
  }
  $('stage').toggleAttribute('data-stale', stale);
  syncGizmo();
}

// orientation means nothing without a model, or once the page can no longer follow it
function syncGizmo() {
  const state = $('link').dataset.state;
  $('gizmo').hidden = !current || state === 'ended' || state === 'denied';
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
}

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
  $('failure-error').textContent = `${failure.error.code}: ${failure.error.message}`;
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
    if (response.status === 204) throw new Superseded();
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const data = await response.json();
    await loadMeshes(data.parts);
    if (revision !== wanted) return; // a newer revision arrived meanwhile
    clearGroup(model, { ownsGeometry: false });
    for (const part of data.parts) model.add(partObject(part, geometries.get(part.mesh.id)));
    current = data;
    rebuildHelpers(data.bbox);
    renderPartList(data.parts);
    showRevision(data);
    notice(null);
    syncGizmo();
    if (!fitted) {
      fitted = true;
      enableCamera(true);
      applyView('iso');
    }
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
  if (token) {
    // the token lives only in the fragment; trade it for an HttpOnly cookie, then drop it.
    await fetch('/api/session', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token }),
    }).catch(() => null);
    history.replaceState(null, '', location.pathname);
  }
  if (await authorized() !== true) {
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

// true or false; null while the server is unreachable
async function authorized() {
  try {
    const response = await fetch('/api/session', { cache: 'no-store' });
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

// a restarted server has a new key: ask whether our cookie still works before reconnecting
async function lost() {
  setLink('lost', 'Not live');
  if (!current) notice('Disconnected from coalesce', 'This page reconnects by itself when coalesce runs again.');
  const access = await authorized();
  if (access === null) {
    setTimeout(lost, 2000);
  } else if (access) {
    listen();
  } else {
    setLink('ended', 'Link expired');
    if (!current) notice(NEW_LINK, NEW_LINK_HELP);
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
      meshes: current?.parts.map((part) => part.mesh.id) ?? [],
      geometries: model.children.map((group) => group.children[0].geometry.uuid),
      camera: controls.enabled,
    }),
  });
}

requestRender();
connect();
