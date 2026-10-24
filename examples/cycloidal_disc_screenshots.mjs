import path from 'node:path';

const [modulePath, url, output, stage, revisionText] = process.argv.slice(2);
const revision = Number(revisionText);
const { chromium } = await import(modulePath);
const browser = await chromium.launch();
const errors = [];

try {
  for (const [size, width, height] of [['1280x800', 1280, 800], ['390x844', 390, 844]]) {
    const page = await browser.newPage({ viewport: { width, height }, deviceScaleFactor: 2 });
    try {
      page.on('console', message => message.type() === 'error' && errors.push(`${size} console: ${message.text()}`));
      page.on('pageerror', error => errors.push(`${size} page: ${error}`));
      page.on('requestfailed', request => errors.push(`${size} request: ${request.url()} ${request.failure()?.errorText}`));
      await page.addInitScript(() => { window.coalesceDebug = true; });
      await page.goto(url);
      await page.waitForFunction(
        expected => window.coalesceViewer?.drawn?.revision === expected,
        revision,
        { timeout: 30000 },
      );
      await page.screenshot({ path: path.join(output, `${stage}-${size}.png`) });
    } finally {
      await page.close();
    }
  }

  if (errors.length) throw new Error(errors.join('\n'));
} finally {
  await browser.close();
}
