import fs from 'node:fs';
import path from 'node:path';
import readline from 'node:readline';

const [modulePath, url, screenshots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const browser = await chromium.launch();
const errors = [];
fs.mkdirSync(screenshots, { recursive: true });

async function open(name, width, height) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 });
  page.on('console', (message) => message.type() === 'error' && errors.push(`${name}: ${message.text()}`));
  page.on('pageerror', (error) => errors.push(`${name}: ${error}`));
  await page.addInitScript(() => { window.coalesceDebug = true; });
  await page.goto(url);
  return { name, page };
}

const pages = [await open('1280x800', 1280, 800), await open('390x844', 390, 844)];
const rowFor = (page) => page.locator('.fit-row').filter({ has: page.locator('.fit-label', { hasText: 'Lid outline' }) });

async function settled(page, revision) {
  await page.waitForFunction((value) => window.coalesceViewer.drawn.revision === value, revision, { timeout: 30000 });
}

async function stale({ name, page }) {
  await settled(page, 5);
  const row = rowFor(page);
  await row.scrollIntoViewIfNeeded();
  await row.focus();
  await page.screenshot({ path: path.join(screenshots, `${name}.png`) });
  const panelEdge = await page.locator('.panel').evaluate((panel) => panel.getBoundingClientRect().right);
  return {
    status: await row.locator('.fit-status').textContent(),
    label: await row.locator('.fit-label').textContent(),
    value: await row.locator('.fit-value').textContent(),
    reason: await row.locator('.fit-reason').textContent(),
    focused: await row.evaluate((element) => element === document.activeElement),
    readable: await row.evaluate((element, edge) => {
      const box = element.getBoundingClientRect();
      return box.left >= 0 && box.right <= edge && element.scrollWidth <= element.clientWidth;
    }, panelEdge),
  };
}

async function clear({ page }) {
  await settled(page, 6);
  return { count: await rowFor(page).count() };
}

const steps = { stale, clear };
for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  const answer = {};
  for (const each of pages) answer[each.name] = await steps[line.trim()](each);
  console.log(JSON.stringify({ ...answer, errors }));
}
await browser.close();
