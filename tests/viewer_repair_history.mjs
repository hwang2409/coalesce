import fs from 'node:fs';
import path from 'node:path';

const [modulePath, url, shots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
fs.mkdirSync(shots, { recursive: true });
const browser = await chromium.launch();
const page = await browser.newPage({ viewport: { width: 1280, height: 800 } });
const errors = [];
page.on('console', (message) => { if (message.type() === 'error') errors.push(message.text()); });
page.on('pageerror', (error) => errors.push(String(error)));
await page.goto(url);
await page.locator('#repair-history-rows').getByText('1000 mm → 1 mm').waitFor();

const summary = page.locator('#repair-history summary');
await summary.focus();
await page.keyboard.press('Enter');
if (await page.locator('#repair-history-rows').isVisible()) throw new Error('keyboard did not close history');
await page.keyboard.press('Enter');
await page.locator('#repair-history-rows').getByText('1000 mm → 1 mm').waitFor();
await page.screenshot({ path: path.join(shots, 'repair-history-1280x800.png') });

await page.reload();
await page.locator('#repair-history-rows').getByText('1000 mm → 1 mm').waitFor();
if (await page.locator('#repair-history-rows li').count() !== 1) throw new Error('reload invented history');
await page.setViewportSize({ width: 390, height: 844 });
await page.locator('#repair-history').scrollIntoViewIfNeeded();
const layout = await page.evaluate(() => ({ width: document.documentElement.scrollWidth, viewport: innerWidth }));
if (layout.width > layout.viewport) throw new Error(`mobile overflow: ${JSON.stringify(layout)}`);
await page.screenshot({ path: path.join(shots, 'repair-history-390x844.png') });

console.log(JSON.stringify({
  history: await page.locator('#repair-history').textContent(),
  rows: await page.locator('#repair-history-rows li').count(),
  errors,
}));
await browser.close();
