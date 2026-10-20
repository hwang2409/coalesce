// Browser driver for test_viewer_review_flow.py.
// Usage: node viewer_review_smoke.mjs <playwright module path> <viewer url> <width> <height> [screenshot dir]
// Protocol: reads one JSON command per stdin line, {name, wait?, do?, flat?}, and answers with
// "step <name> <json>": what the page shows after the wait and the actions.
import readline from 'node:readline';

const [modulePath, url, width, height, shots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const size = `${width}x${height}`;
const touch = Number(width) < 720; // the narrow layout stands for a phone: taps, not clicks
const context = await browser.newContext({
  viewport: { width: Number(width), height: Number(height) },
  deviceScaleFactor: 2,
  hasTouch: touch,
  permissions: ['clipboard-read', 'clipboard-write'],
});
const page = await context.newPage();
await page.addInitScript(() => { window.coalesceDebug = true; });
const errors = [];
page.on('pageerror', (error) => errors.push(String(error)));
page.on('console', (message) => message.type() === 'error' && errors.push(message.text()));
page.on('requestfailed', (request) => errors.push(`${request.method()} ${new URL(request.url()).pathname} ${request.failure()?.errorText}`));

const shown = async (selector) => ((await page.locator(selector).isVisible()) ? page.locator(selector).innerText() : null);
const waitForText = async (text) => {
  const started = Date.now();
  try {
    await page.getByText(text, { exact: true }).first().waitFor({ timeout: 30000 });
  } catch (error) {
    const link = await page.locator('#link-text').textContent().catch(() => null);
    const status = await shown('#status').catch(() => null);
    throw new Error(`${error.message}; elapsed=${Date.now() - started}ms link=${JSON.stringify(link)} status=${JSON.stringify(status)}`);
  }
};
const settled = () => page.waitForFunction(() => window.coalesceViewer.drawn.settled, null, { timeout: 15000 });

// what a sighted or screen-reader user gets from the page right now
async function snapshot() {
  return {
    revision: await page.evaluate(() => window.coalesceViewer.revision),
    link: await page.locator('#link-text').textContent(),
    change: await shown('#change'),
    parts: await page.locator('#parts li').allInnerTexts(),
    names: await page.locator('.part-name').allTextContents(),
    pressed: await page.locator('#parts [aria-pressed]').evaluateAll((controls) => controls.map((c) => c.getAttribute('aria-pressed'))),
    details: await page.locator('.part-detail').evaluateAll((lines) => lines.map((line) => (line.checkVisibility() ? line.textContent : null))),
    description: await page.locator('#view-description').textContent(),
    scale: await shown('#scale'),
    status: await shown('#status'),
    repair: await shown('#failure-repair'),
    code: await page.locator('#failure-code').textContent(),
    scene: await page.evaluate(() => window.coalesceViewer.drawn),
    expanded: await page.locator('#parts .part').evaluateAll((rows) => rows.map((row) => row.getAttribute('aria-expanded'))),
    // each part row's boxes in page pixels, to check the swatch and hint line up with the row
    rows: await page.locator('#parts li').evaluateAll((items) => items.map((item) => {
      const box = (el) => (el && el.checkVisibility() ? el.getBoundingClientRect().toJSON() : null);
      const hint = item.querySelector('.disclosure');
      return {
        row: box(item),
        toggle: box(item.querySelector('.visibility')),
        part: box(item.querySelector('.part')),
        swatch: box(item.querySelector('.swatch')),
        name: box(item.querySelector('.part-name')),
        hint: box(hint),
        hintColor: hint && getComputedStyle(hint).color,
      };
    })),
    focus: await page.evaluate(() => {
      const style = getComputedStyle(document.activeElement);
      return { className: document.activeElement.className, outline: `${style.outlineStyle} ${style.outlineWidth}` };
    }),
  };
}

// pixels inside a region of the model's top view that differ from the region's median color.
// the region is given as fractions of the visible box in x and y; a flat face must be one color.
async function flat({ fx, fy }) {
  return page.evaluate(({ fx, fy }) => {
    const { projected } = window.coalesceViewer.drawn;
    const xs = projected.map((p) => p[0]);
    const ys = projected.map((p) => p[1]);
    const source = document.getElementById('view');
    const copy = document.createElement('canvas');
    copy.width = source.width;
    copy.height = source.height;
    const context = copy.getContext('2d');
    context.drawImage(source, 0, 0);
    const toX = (f) => Math.round(((Math.min(...xs) + f * (Math.max(...xs) - Math.min(...xs)) + 1) / 2) * copy.width);
    const toY = (f) => Math.round(((1 - (Math.min(...ys) + f * (Math.max(...ys) - Math.min(...ys)))) / 2) * copy.height);
    const [x0, x1, y0, y1] = [toX(fx[0]), toX(fx[1]), toY(fy[1]), toY(fy[0])];
    const { data } = context.getImageData(x0, y0, x1 - x0, y1 - y0);
    const sums = [];
    for (let i = 0; i < data.length; i += 4) sums.push(data[i] * 65536 + data[i + 1] * 256 + data[i + 2]);
    const median = [...sums].sort((a, b) => a - b)[sums.length >> 1];
    const [mr, mg, mb] = [median >> 16, (median >> 8) & 255, median & 255];
    let off = 0;
    for (let i = 0; i < data.length; i += 4) {
      if (Math.abs(data[i] - mr) + Math.abs(data[i + 1] - mg) + Math.abs(data[i + 2] - mb) > 24) off += 1;
    }
    return { pixels: sums.length, off, median: [mr, mg, mb] };
  }, { fx, fy });
}

const label = () => page.evaluate(() => {
  const el = document.activeElement;
  if (el === document.body) return 'body';
  return (el.querySelector('.part-name') ?? el).textContent.trim();
});

const press = (locator) => (touch ? locator.tap() : locator.click());

async function act(action) {
  if ('focusPart' in action) await page.locator('.part').nth(action.focusPart).focus();
  if ('tapPart' in action) await press(page.locator('.part').nth(action.tapPart));
  if ('toggle' in action) await press(page.locator('#parts li').nth(action.toggle).locator('.visibility'));
  if ('rejectClipboard' in action) {
    await page.evaluate(() => { navigator.clipboard.writeText = () => Promise.reject(new DOMException('denied', 'NotAllowedError')); });
  }
  if ('click' in action) await page.getByRole('button', { name: action.click, exact: true }).click();
  if ('until' in action) await page.getByText(action.until, { exact: true }).waitFor({ timeout: 5000 });
  if ('blur' in action) await page.evaluate(() => document.activeElement?.blur());
  if ('stallSession' in action) {
    const sessionUrl = new URL('/api/session', url).href;
    await page.route(sessionUrl, () => new Promise(() => {}));
  }
  // the drawn edge-line corners on one circle (center x, y and radius in mm), counted per height
  if ('circle' in action) {
    return page.evaluate(async ({ mesh, center: [cx, cy], radius }) => {
      const buffer = await (await fetch(mesh.url)).arrayBuffer();
      const lines = new Float32Array(buffer, (mesh.vertexCount + mesh.triangleCount) * 12, mesh.edgeVertexCount * 3);
      const levels = {};
      for (let i = 0; i < lines.length; i += 3) {
        if (Math.abs(Math.hypot(lines[i] - cx, lines[i + 1] - cy) - radius) > 1e-3) continue;
        const z = lines[i + 2].toFixed(3);
        (levels[z] ??= new Set()).add(`${lines[i].toFixed(4)},${lines[i + 1].toFixed(4)}`);
      }
      return { corners: Object.values(levels).map((corners) => corners.size) };
    }, action.circle);
  }
  if ('goto' in action) {
    await page.goto(action.goto);
    await page.waitForFunction((n) => window.coalesceViewer?.revision === n, action.revision, { timeout: 15000 });
    await settled();
  }
  // keyboard only: Tab until the named control has focus, then press the key
  if ('tabTo' in action) {
    const visited = [];
    for (let i = 0; i < 40 && (visited.at(-1) ?? '') !== action.tabTo; i += 1) {
      await page.keyboard.press('Tab');
      visited.push(await label());
    }
    if (action.press) await page.keyboard.press(action.press);
    return { visited };
  }
  return {};
}

await page.goto(url);
for await (const line of readline.createInterface({ input: process.stdin })) {
  const { name, wait = {}, do: actions = [], flat: region } = JSON.parse(line);
  if (wait.revision) await page.waitForFunction((n) => window.coalesceViewer?.revision === n, wait.revision, { timeout: 15000 });
  if (wait.failure) await page.waitForFunction((code) => window.coalesceViewer.failure?.error.code === code, wait.failure, { timeout: 15000 });
  if (wait.text) await waitForText(wait.text);
  if (wait.ready) await page.getByText('Waiting for the first revision').waitFor({ timeout: 15000 });
  const results = [];
  for (const action of actions) results.push(await act(action));
  if (wait.revision || wait.failure) await settled();
  const result = { ...(await snapshot()), actions: results };
  if (region) result.flat = await flat(region);
  if (actions.some((action) => 'click' in action)) result.clipboard = await page.evaluate(() => navigator.clipboard.readText()).catch(() => null);
  if (shots) await page.screenshot({ path: `${shots}/${size}-${name}.png` });
  console.log(`step ${name} ${JSON.stringify({ ...result, errors: errors.splice(0) })}`);
}
await browser.close();
