// Browser smoke test driver for tests/test_viewer_browser.py.
// Usage: node viewer_smoke.mjs <playwright module path> <viewer url> [screenshot dir]
// Protocol: prints "step <name> <json>" after each check and waits for one stdin line
// ("next") before the following step, so the Python side can publish between steps.
import readline from 'node:readline';

const [modulePath, url, shots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const lines = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();

const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 });
await page.addInitScript(() => { window.coalesceDebug = true; });
const errorsOf = (target) => {
  const found = [];
  target.on('pageerror', (error) => found.push(String(error)));
  target.on('console', (message) => message.type() === 'error' && found.push(message.text()));
  return found;
};

// share of canvas pixels covered by model edge lines, the topmost such row (canvas and
// page device px), and the scene the page says it drew; call after settled()
async function drawn() {
  return page.evaluate(() => {
    const source = document.getElementById('view');
    const offset = source.getBoundingClientRect().top * devicePixelRatio;
    const copy = document.createElement('canvas');
    copy.width = source.width;
    copy.height = source.height;
    const context = copy.getContext('2d');
    context.drawImage(source, 0, 0);
    const { data } = context.getImageData(0, 0, copy.width, copy.height);
    let dark = 0;
    let top = copy.height;
    for (let i = 0; i < data.length; i += 4) {
      // only the model's B-rep edge lines (#1d2329) are this dark; grid and axes are lighter
      if (data[i] + data[i + 1] + data[i + 2] < 200) {
        dark += 1;
        top = Math.min(top, Math.floor(i / 4 / copy.width));
      }
    }
    return { dark: dark / (data.length / 4), top, pageTop: top + offset, scene: window.coalesceViewer.drawn };
  });
}

const errors = errorsOf(page);
async function step(name, check) {
  const result = await check();
  if (shots) await page.screenshot({ path: `${shots}/${name}.png` });
  // console errors since the previous step
  console.log(`step ${name} ${JSON.stringify({ ...result, errors: errors.splice(0) })}`);
  await lines.next();
}

const STRESS_LAST = 52; // tests/test_viewer_browser.py publishes revisions 3..52 after 'front'
const revision = () => page.getByTestId('revision');
const controls = () => page.locator('.views button').evaluateAll((buttons) => buttons.map((b) => b.disabled));
const shown = async (selector) => ((await page.locator(selector).isVisible()) ? page.locator(selector).innerText() : null);
const gizmo = () => page.locator('.gizmo').isVisible();
const stale = async () => ({
  link: await page.locator('#link-text').textContent(),
  banner: await shown('#stale'),
  filter: await page.locator('#view').evaluate((canvas) => getComputedStyle(canvas).filter),
  failureColor: await page.locator('#failure-title').evaluate((el) => getComputedStyle(el).color),
  gizmo: await gizmo(),
  ...(await layout()),
});
// css px: the canvas must fill the stage below the status row, never under it
const layout = () => page.evaluate(() => {
  const view = document.getElementById('view').getBoundingClientRect();
  const status = document.getElementById('status').getBoundingClientRect();
  return { viewTop: view.top, viewBottom: view.bottom, statusBottom: status.bottom };
});
let eventRequests = 0;
page.on('request', (request) => new URL(request.url()).pathname === '/events' && (eventRequests += 1));
const waitRevision = (n) => page.waitForFunction((n) => window.coalesceViewer?.revision === n, n, { timeout: 15000 });
// the canvas has its final size and position and a frame for them is drawn
const settled = () => page.waitForFunction(() => window.coalesceViewer.drawn.settled, null, { timeout: 15000 });

await page.goto(url);
await step('empty', async () => {
  await page.getByText('Waiting for the first revision').waitFor({ timeout: 15000 });
  if (shots) await page.locator('.views').screenshot({ path: `${shots}/controls-disabled.png` });
  return { label: await revision().textContent(), link: await page.locator('#link-text').textContent(), disabled: await controls(), gizmo: await gizmo() };
});
await waitRevision(1);
await settled();
await step('revision-1', async () => ({
  label: await revision().textContent(),
  url: page.url(),
  disabled: await controls(),
  gizmo: await gizmo(),
  ...(await drawn()),
}));
await waitRevision(2);
await settled();
await step('revision-2', async () => ({
  label: await revision().textContent(),
  ...(await layout()),
  memory: await page.evaluate(() => window.coalesceViewer.memory),
  ...(await drawn()),
}));
await page.locator('#failure').waitFor({ state: 'visible', timeout: 15000 });
await settled();
await step('failure', async () => ({
  label: await revision().textContent(),
  failure: await page.locator('#failure').innerText(),
  failureColor: await page.locator('#failure-title').evaluate((el) => getComputedStyle(el).color),
  ...(await drawn()),
}));
await page.getByRole('button', { name: 'Front' }).click();
await settled();
await step('front', async () => ({ pressed: await page.getByRole('button', { name: 'Front' }).getAttribute('aria-pressed'), ...(await drawn()) }));
await page.getByRole('button', { name: 'Iso' }).click();
await settled();
const iso = await drawn(); // the model's place before the connection drops
await page.locator('#stale').waitFor({ state: 'visible', timeout: 15000 });
await settled();
await page.waitForTimeout(300); // let the dim transition finish before the screenshot
await step('disconnected', async () => ({ ...(await stale()), before: iso.pageTop, ...(await drawn()) }));
await page.locator('#stale').waitFor({ state: 'hidden', timeout: 15000 });
await settled();
await page.waitForTimeout(300);
await step('reconnected', async () => ({ ...(await stale()), before: iso.pageTop, ...(await drawn()) }));
await waitRevision(STRESS_LAST);
await page.waitForTimeout(300);
await step('stress', async () => ({ memory: await page.evaluate(() => window.coalesceViewer.memory) }));
// the Python side restarts the server with a new key; the old page must say so and stop retrying
await page.getByText('Open the new viewer link').waitFor({ timeout: 20000 });
await page.waitForTimeout(300);
const requestsAtEnd = eventRequests;
await page.waitForTimeout(5000); // longer than the EventSource retry delay
await step('new-link', async () => ({
  ...(await stale()),
  status: await shown('#status'),
  eventRequestsAfter: eventRequests - requestsAtEnd,
  disabled: await controls(),
  camera: await page.evaluate(() => window.coalesceViewer.drawn.camera),
}));
// a fresh browser profile without the key: no cookie, no #token fragment
const bare = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 });
const bareErrors = errorsOf(bare);
await bare.goto(new URL(url).origin + '/');
await bare.getByText('Open the viewer link that coalesce printed').waitFor({ timeout: 15000 });
await bare.waitForTimeout(300);
if (shots) await bare.screenshot({ path: `${shots}/no-access.png` });
console.log(`step no-access ${JSON.stringify({
  link: await bare.locator('#link-text').textContent(),
  meta: await bare.locator('#revision-meta').textContent(),
  notice: await bare.locator('#notice-body').textContent(),
  revisionVisible: await bare.locator('.revision-line').isVisible(),
  partsVisible: await bare.locator('.parts').isVisible(),
  gizmo: await bare.locator('.gizmo').isVisible(),
  errors: bareErrors,
})}`);
await lines.next();
await browser.close();
process.exit(0); // stdin stays open otherwise
