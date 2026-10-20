// Browser driver for test_viewer_browser.py::test_viewer_frames_fails_and_ends_honestly.
// Usage: node viewer_ux_smoke.mjs <playwright module path> <viewer url> <width> <height> [screenshot dir]
// Protocol: prints "step <name> <json>" after each check and waits for one stdin line
// ("next") before the following step, so the Python side can publish between steps.
import readline from 'node:readline';

const [modulePath, url, width, height, shots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const lines = readline.createInterface({ input: process.stdin })[Symbol.asyncIterator]();

const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const size = `${width}x${height}`;
const page = await browser.newPage({ viewport: { width: Number(width), height: Number(height) }, deviceScaleFactor: 2 });
await page.addInitScript(() => { window.coalesceDebug = true; });
const errors = [];
page.on('pageerror', (error) => errors.push(String(error)));
page.on('console', (message) => message.type() === 'error' && errors.push(message.text()));
page.on('requestfailed', (request) => errors.push(`${request.method()} ${new URL(request.url()).pathname} ${request.failure()?.errorText}`));
const requests = [];
page.on('request', (request) => requests.push(new URL(request.url()).pathname));

// canvas pixels: share far from the background (model surfaces and edges; the light grid is
// excluded), share of dark B-rep edge pixels, and the scene the page says it drew
async function drawn() {
  return page.evaluate(() => {
    const source = document.getElementById('view');
    const copy = document.createElement('canvas');
    copy.width = source.width;
    copy.height = source.height;
    const context = copy.getContext('2d');
    context.drawImage(source, 0, 0);
    const { data } = context.getImageData(0, 0, copy.width, copy.height);
    let model = 0;
    let dark = 0;
    for (let i = 0; i < data.length; i += 4) {
      const sum = data[i] + data[i + 1] + data[i + 2];
      if (sum < 545) model += 1; // background #e4e8eb sums to 695, grid lines to 488..620
      if (sum < 200) dark += 1;
    }
    const pixels = data.length / 4;
    return { coverage: model / pixels, dark: dark / pixels, scene: window.coalesceViewer.drawn };
  });
}

async function step(name, check) {
  const result = await check();
  if (shots) await page.screenshot({ path: `${shots}/${size}-${name}.png` });
  console.log(`step ${name} ${JSON.stringify({ ...result, errors: errors.splice(0) })}`);
  await lines.next();
}

const shown = async (selector) => ((await page.locator(selector).isVisible()) ? page.locator(selector).innerText() : null);
const settled = () => page.waitForFunction(() => window.coalesceViewer.drawn.settled, null, { timeout: 15000 });
const waitRevision = (n) => page.waitForFunction((n) => window.coalesceViewer?.revision === n, n, { timeout: 15000 });
const texts = async () => ({
  link: await page.locator('#link-text').textContent(),
  notice: await shown('#notice'),
  status: await shown('#status'),
  scale: await shown('#scale'),
  fit: await page.locator('#fit').innerText(),
  fitDisabled: await page.locator('#fit').isDisabled(),
});

await page.goto(url);
await page.getByText('Waiting for the first revision').waitFor({ timeout: 15000 });
await step('empty', async () => ({
  parts: await page.locator('#parts').innerText(),
  // the 3d view is a named, described object in the accessibility tree
  aria: await page.locator('main').ariaSnapshot(),
  ...(await texts()),
}));

await waitRevision(1);
await settled();
await step('no-solids', async () => ({ ...(await texts()), parts: await page.locator('#parts').innerText(), ...(await drawn()) }));

await waitRevision(2);
await settled();
await step('plate', async () => ({
  ...(await texts()),
  description: await page.locator('#view-description').textContent(),
  ...(await drawn()),
}));

await page.locator('#failure').waitFor({ state: 'visible', timeout: 15000 });
await settled();
await step('failure', async () => ({
  ...(await texts()),
  repair: await page.locator('#failure-repair').innerText(),
  codeVisible: await page.locator('#failure-code').isVisible(),
  ...(await drawn()),
}));
await page.locator('.failure-details summary').click();
await step('failure-details', async () => ({ codeVisible: await page.locator('#failure-code').isVisible() }));

await waitRevision(3);
await settled();
await step('large', async () => ({ ...(await texts()), ...(await drawn()) }));

const long = page.locator('.part').nth(1);
await long.focus();
await step('long-name-focus', async () => ({
  name: await long.locator('.part-name').evaluate((el) => ({ text: el.textContent, clipped: el.scrollWidth > el.clientWidth })),
}));
await page.locator('#view').focus(); // blur the part row again

// the human orbits: their view must survive the next revision
const box = await page.locator('#view').boundingBox();
await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
await page.mouse.down();
await page.mouse.move(box.x + box.width / 2 + 60, box.y + box.height / 2 + 20, { steps: 5 });
await page.mouse.up();
await settled();
await step('orbited', drawn);

await waitRevision(4);
await settled();
await step('held', async () => ({ ...(await texts()), ...(await drawn()) }));
await page.locator('#fit').click();
await settled();
await step('refit', async () => ({ ...(await texts()), ...(await drawn()) }));

// the Python side stops the server for good: a brief drop first, then a terminal state
await page.locator('#stale').waitFor({ state: 'visible', timeout: 15000 });
await settled();
await step('reconnecting', async () => ({ ...(await texts()), ...(await drawn()) }));
await page.getByText('Viewer ended', { exact: true }).waitFor({ timeout: 30000 });
const before = requests.length;
await page.waitForTimeout(5000); // longer than the 2 s retry delay
await step('ended', async () => ({
  ...(await texts()),
  requestsAfter: requests.slice(before),
  buttons: await page.locator('.views button').evaluateAll((buttons) => buttons.map((b) => b.disabled)),
  ...(await drawn()),
}));
await browser.close();
process.exit(0); // stdin stays open otherwise
