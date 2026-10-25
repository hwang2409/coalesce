import path from 'node:path';

const [modulePath, url, output, revisionText] = process.argv.slice(2);
const revision = Number(revisionText);
const { chromium } = await import(modulePath);
const browser = await chromium.launch();
const errors = [];

try {
  for (const [name, width, height] of [['desktop', 1280, 800], ['phone', 390, 844]]) {
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 });
    try {
      page.on('console', message => message.type() === 'error' && errors.push(`${name} console: ${message.text()}`));
      page.on('pageerror', error => errors.push(`${name} page: ${error}`));
      page.on('requestfailed', request => errors.push(`${name} request: ${request.url()} ${request.failure()?.errorText}`));
      await page.addInitScript(() => { window.coalesceDebug = true; });
      await page.goto(url);
      await page.waitForFunction(expected => window.coalesceViewer?.drawn?.revision === expected, revision, { timeout: 30000 });
      await page.waitForSelector('.assembly-row');
      await page.screenshot({ path: path.join(output, `${name}-assembled.png`), fullPage: true });
      await page.locator('.assembly-row').first().click();
      await page.screenshot({ path: path.join(output, `${name}-pair-focused.png`), fullPage: true });
    } finally {
      await page.close();
    }
  }
  if (errors.length) throw new Error(errors.join('\n'));
} finally {
  await browser.close();
}
