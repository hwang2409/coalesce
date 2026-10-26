const path = await import('node:path');
const [modulePath, url, output, revisionText] = process.argv.slice(2);
const revision = Number(revisionText);
const { chromium } = await import(modulePath);
const browser = await chromium.launch();
const errors = [];

try {
  const desktop = await browser.newPage({ viewport: { width: 1280, height: 800 }, deviceScaleFactor: 2 });
  try {
    desktop.on('console', message => message.type() === 'error' && errors.push(`desktop console: ${message.text()}`));
    desktop.on('pageerror', error => errors.push(`desktop page: ${error}`));
    desktop.on('requestfailed', request => errors.push(`desktop request: ${request.url()} ${request.failure()?.errorText}`));
    await desktop.addInitScript(() => { window.coalesceDebug = true; });
    await desktop.goto(url);
    await desktop.waitForFunction(expected => window.coalesceViewer?.drawn?.revision === expected, revision, { timeout: 30000 });
    await desktop.waitForSelector('[data-mate-id="pin-1-axis"]');
    if (await desktop.locator('#panel-scroll').evaluate(node => node.scrollWidth > node.clientWidth)) throw new Error('desktop panel overflows horizontally');
    await desktop.screenshot({ path: path.join(output, 'desktop-assembled-r3.png') });
    await desktop.locator('[data-mate-id="pin-1-axis"]').click();
    await desktop.screenshot({ path: path.join(output, 'desktop-pin-mate-focused-r3.png') });
  } finally {
    await desktop.close();
  }

  const phone = await browser.newPage({ viewport: { width: 390, height: 844 }, deviceScaleFactor: 2 });
  try {
    phone.on('console', message => message.type() === 'error' && errors.push(`phone console: ${message.text()}`));
    phone.on('pageerror', error => errors.push(`phone page: ${error}`));
    phone.on('requestfailed', request => errors.push(`phone request: ${request.url()} ${request.failure()?.errorText}`));
    await phone.addInitScript(() => { window.coalesceDebug = true; });
    await phone.goto(url);
    await phone.waitForFunction(expected => window.coalesceViewer?.drawn?.revision === expected, revision, { timeout: 30000 });
    await phone.waitForSelector('#assembly-state:not([hidden])');
    if (await phone.locator('#panel-scroll').evaluate(node => node.scrollWidth > node.clientWidth)) throw new Error('phone panel overflows horizontally');
    await phone.screenshot({ path: path.join(output, 'phone-r3.png') });
  } finally {
    await phone.close();
  }
  if (errors.length) throw new Error(errors.join('\n'));
} finally {
  await browser.close();
}
