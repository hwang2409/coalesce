// Screw-closure evidence in a real browser on a live stdio coalesce.
import fs from 'node:fs';
import path from 'node:path';

const [modulePath, url, screenshots] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const browser = await chromium.launch();
const errors = [];
fs.mkdirSync(screenshots, { recursive: true });

for (const [name, width, height] of [['1280x800', 1280, 800], ['390x844', 390, 844]]) {
  const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 });
  await page.addInitScript(() => { window.coalesceDebug = true; });
  page.on('console', (message) => message.type() === 'error' && errors.push(`${name}: ${message.text()}`));
  page.on('pageerror', (error) => errors.push(`${name}: ${error}`));
  await page.goto(url);
  await page.waitForFunction(() => window.coalesceViewer?.revision === 5 && window.coalesceViewer.drawn?.settled,
    null, { timeout: 30000 });
  const row = page.locator('.fit-row').filter({ hasText: 'Screw closure' });
  await row.scrollIntoViewIfNeeded();
  await page.locator('.fit-evidence > summary').click();
  const pointLines = page.locator('#inspection-evidence li[data-point]');
  await pointLines.first().waitFor();
  await pointLines.last().scrollIntoViewIfNeeded();
  await page.screenshot({ path: path.join(screenshots, `${name}-screw-closure-evidence.png`) });
  const result = {
    row: await row.innerText(),
    points: await pointLines.allTextContents(),
    visible: await pointLines.evaluateAll((lines) => lines.every((line) => {
      const box = line.getBoundingClientRect();
      return box.top >= 0 && box.bottom <= window.innerHeight;
    })),
  };
  console.log(JSON.stringify({ name, result }));
  await page.close();
}
console.log(JSON.stringify({ errors }));
await browser.close();
