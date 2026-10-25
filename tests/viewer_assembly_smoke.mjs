const [, , modulePath, url, width, height, shots, refreshTrigger] = process.argv;
const { writeFile } = await import('node:fs/promises');
const { chromium } = await import(modulePath);
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: Number(width), height: Number(height) } });
await page.addInitScript(() => { window.coalesceDebug = true; });
const errors = [];
page.on('console', message => { if (message.type() === 'error') errors.push(message.text()); });
page.on('pageerror', error => errors.push(error.message));
await page.goto(url);
try {
  await page.waitForSelector('[data-testid="revision"]:has-text("1")', { timeout: 10000 });
} catch (error) {
  console.error(JSON.stringify({ errors, body: await page.locator('body').innerText() }));
  throw error;
}
await page.waitForSelector('.assembly-row');
const state = async () => page.evaluate(() => ({
  names: [...document.querySelectorAll('.part-name')].map(node => node.textContent),
  details: [...document.querySelectorAll('.part-detail')].map(node => node.textContent),
  controls: [...document.querySelectorAll('button')].filter(node => !node.disabled).map(node => node.getAttribute('aria-label') || node.textContent.trim()),
  visible: [...document.querySelectorAll('.visibility')].map(node => node.getAttribute('aria-pressed')),
  pairPressed: document.querySelector('.assembly-row')?.getAttribute('aria-pressed'),
  showing: document.querySelector('#showing-text')?.textContent,
  description: document.querySelector('#view-description')?.textContent,
  memory: window.coalesceViewer.memory,
  worldBounds: window.coalesceViewer.drawn.worldBounds,
  inspectionCount: document.querySelector('#inspection-count')?.textContent,
  inspectionStatuses: [...document.querySelectorAll('.assembly-row')].map(node => node.dataset.status),
  inspectionValues: [...document.querySelectorAll('.assembly-row .fit-value')].map(node => node.textContent),
  inspectionReasons: [...document.querySelectorAll('.assembly-row .fit-reason')].map(node => node.textContent),
  morePassing: document.querySelector('.assembly-more button')?.textContent ?? null,
  partNames: [...document.querySelectorAll('.part')].map(node => node.getAttribute('aria-label')),
  partSizes: [...document.querySelectorAll('.part-size')].map(node => node.textContent),
  rawInspection: document.querySelector('#inspection-raw')?.textContent,
}));
const initial = await state();
if (shots) await page.screenshot({ path: `${shots}/${width}-assembled-r2.png`, fullPage: true });
const firstToggle = page.locator('.visibility').first();
await firstToggle.click();
const hidden = await state();
await firstToggle.click();
await page.locator('.assembly-row').first().click();
const focused = await state();
if (shots) await page.screenshot({ path: `${shots}/${width}-pair-focused-r2.png`, fullPage: true });
await page.locator('.assembly-more button').focus();
await page.keyboard.press('Enter');
const expandedPassing = await page.locator('.assembly-row[data-status="PASS"]').count();
await writeFile(refreshTrigger, 'ready');
await page.waitForFunction(() => document.querySelector('.assembly-row .fit-label')?.textContent === 'pin-left ↔ pin-right');
const refreshed = await state();
await page.locator('.assembly-row').first().click();
await page.locator('.assembly-row').first().click();
const restored = await state();
await page.locator('.assembly-row').first().click();
await page.keyboard.press('Escape');
const escaped = await state();
await page.getByRole('button', { name: 'Section' }).click();
const section = await state();
console.log(JSON.stringify({ initial, expandedPassing, hidden, focused, refreshed, restored, escaped, section, errors }));
await browser.close();
