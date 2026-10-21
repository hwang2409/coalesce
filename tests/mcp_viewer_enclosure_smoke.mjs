// The enclosure review flow in a real browser on a live stdio coalesce: one desktop and one phone page.
// Each stdin line names a step; the driver answers one JSON line per step. Every element is scrolled into
// view and activated before its screenshot, so a screenshot shows the state the step asserts.
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

async function settled(page, revision) {
  await page.waitForFunction((r) => {
    const drawn = window.coalesceViewer.drawn;
    return drawn.revision === r && drawn.settled;
  }, revision, { timeout: 30000 });
}
const shot = (name, page, step) => page.screenshot({ path: path.join(screenshots, `${name}-${step}.png`) });
const inView = (locator) => locator.evaluate((element) => {
  const box = element.getBoundingClientRect();
  return box.height > 0 && box.top >= 0 && box.bottom <= window.innerHeight;
});
const fitRow = (page, label) => page.locator('.fit-row').filter({ has: page.locator('.fit-label', { hasText: label }) });

async function readFit({ page }) {
  return {
    count: await page.locator('#inspection-count').textContent(),
    rows: await page.locator('.fit-row').evaluateAll((rows) => rows.map((row) => ({
      status: row.querySelector('.fit-status').textContent,
      label: row.querySelector('.fit-label').textContent,
      value: row.querySelector('.fit-value')?.textContent ?? null,
    }))),
  };
}

async function fitGap({ name, page }) {
  await settled(page, 7);
  const row = fitRow(page, 'Lid fit gap');
  await row.scrollIntoViewIfNeeded();
  await shot(name, page, 'lid-fit-gap');
  return { ...(await readFit({ page })), visible: await inView(row) };
}

async function evidence({ name, page }) {
  await page.locator('.fit-evidence > summary').click();
  const list = page.locator('#inspection-evidence');
  await list.locator('li').first().waitFor();
  await page.waitForTimeout(200);
  await shot(name, page, 'evidence');
  return {
    lines: await list.locator('li').allTextContents(),
    firstLineVisible: await inView(list.locator('li').first()),
    rawOpen: await page.locator('.fit-raw').evaluate((details) => details.open),
    rawVisible: await page.locator('#inspection-raw').isVisible(),
  };
}

async function activate({ name, page }, label, how) {
  const before = await page.evaluate(() => window.coalesceViewer.drawn.view);
  const row = fitRow(page, label);
  await row.scrollIntoViewIfNeeded();
  if (how === 'click') await row.click();
  else {
    await row.focus();
    await page.keyboard.press('Enter');
  }
  await settled(page, 7);
  const showing = page.locator('#showing');
  const drawn = () => page.evaluate(() => window.coalesceViewer.drawn);
  const state = {
    pressed: await fitRow(page, label).getAttribute('aria-pressed'),
    canvasLabel: await showing.isVisible() ? await page.locator('#showing-text').textContent() : null,
    canvasLabelInView: await showing.isVisible() && await inView(showing),
    faded: (await drawn()).faded,
    parts: (await drawn()).parts,
    cameraUnchanged: JSON.stringify(before) === JSON.stringify((await drawn()).view),
  };
  await shot(name, page, `fit-row-${how}`);
  // a click clears with a second press on the row, Enter with the canvas label's button
  if (how === 'click') await fitRow(page, label).click();
  else await page.locator('#showing-clear').click();
  await settled(page, 7);
  state.cleared = { faded: (await drawn()).faded, labelHidden: await showing.isHidden(),
    pressed: await fitRow(page, label).getAttribute('aria-pressed') };
  return state;
}

async function facts({ name, page }) {
  const row = page.locator('.part').filter({ hasText: 'Reference' }).first();
  await row.scrollIntoViewIfNeeded();
  await row.click();
  const holes = row.locator('.fact').filter({ hasText: 'Mounting holes' });
  await holes.waitFor();
  await holes.scrollIntoViewIfNeeded();
  await page.waitForTimeout(200);
  await shot(name, page, 'mounting-holes');
  return { text: await holes.locator('.fact-value').textContent(), visible: await inView(holes) };
}

async function failure({ name, page }) {
  await page.locator('#failure').waitFor({ state: 'visible', timeout: 15000 });
  await page.waitForTimeout(200);
  await shot(name, page, 'failure');
  return { code: await page.locator('#failure-code').textContent() };
}

async function repaired({ name, page }) {
  await settled(page, 8);
  await page.locator('#failure').waitFor({ state: 'hidden' });
  const change = page.locator('#change');
  await change.scrollIntoViewIfNeeded();
  await shot(name, page, 'repaired');
  return { change: await change.textContent(), visible: await inView(change) };
}

const steps = {
  built: async (each) => ({ fit: await fitGap(each), evidence: await evidence(each) }),
  rows: async (each) => ({
    click: await activate(each, 'Standoffs', 'click'),
    enter: await activate(each, 'Lid interference', 'enter'),
    facts: await facts(each),
  }),
  failure,
  repaired,
};

for await (const line of readline.createInterface({ input: process.stdin, crlfDelay: Infinity })) {
  const answer = {};
  for (const each of pages) answer[each.name] = await steps[line.trim()](each);
  console.log(JSON.stringify({ ...answer, errors }));
}
await browser.close();
