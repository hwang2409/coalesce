const [modulePath, url, expectedRevision] = process.argv.slice(2);
const { chromium } = await import(modulePath);
const browser = await chromium.launch({ args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader'] });
const page = await browser.newPage();
await page.addInitScript(() => { window.coalesceDebug = true; });
try {
  await page.goto(url);
  await page.locator('#link-text').waitFor({ state: 'visible', timeout: 15000 });
  console.log('ready');
  await page.waitForFunction(() => document.querySelector('#link-text')?.textContent === 'Reconnecting', null, { timeout: 15000 });
  await page.waitForFunction(() => document.querySelector('#link-text')?.textContent === 'Live', null, { timeout: 15000 });
  if (expectedRevision) {
    await page.waitForFunction(
      (revision) => window.coalesceViewer?.revision === Number(revision),
      expectedRevision,
      { timeout: 15000 },
    );
  }
  console.log('reconnected');
} finally {
  await browser.close();
}
