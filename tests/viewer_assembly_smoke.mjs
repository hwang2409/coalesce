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
  pairPressed: document.querySelector('.assembly-row[aria-pressed="true"]')?.getAttribute('aria-pressed') ?? 'false',
  showing: document.querySelector('#showing-text')?.textContent,
  description: document.querySelector('#view-description')?.textContent,
  memory: window.coalesceViewer.memory,
  worldBounds: window.coalesceViewer.drawn.worldBounds,
  colors: window.coalesceViewer.drawn.colors,
  opacities: window.coalesceViewer.drawn.opacities,
  overlapCount: window.coalesceViewer.drawn.overlapCount,
  focusProjected: window.coalesceViewer.drawn.focusProjected,
  selectionEvidence: document.querySelector('#selection-evidence:not([hidden])')?.textContent ?? null,
  inspectionCount: document.querySelector('#inspection-count')?.textContent,
  inspectionStatuses: [...document.querySelectorAll('.assembly-row')].map(node => node.dataset.status),
  inspectionLabels: [...document.querySelectorAll('.assembly-row .fit-label')].map(node => node.textContent),
  inspectionValues: [...document.querySelectorAll('.assembly-row .fit-value')].map(node => node.textContent),
  inspectionReasons: [...document.querySelectorAll('.assembly-row .fit-reason')].map(node => node.textContent),
  morePassing: document.querySelector('.assembly-more button')?.textContent ?? null,
  disclosureExpanded: document.querySelector('.assembly-more button')?.getAttribute('aria-expanded') ?? null,
  activeConnected: document.activeElement?.isConnected ?? false,
  activeKey: document.activeElement?.dataset.key ?? null,
  partNames: [...document.querySelectorAll('.part')].map(node => node.getAttribute('aria-label')),
  partSizes: [...document.querySelectorAll('.part-size')].map(node => node.textContent),
  rawInspection: document.querySelector('#inspection-raw')?.textContent,
  solveStatus: document.querySelector('#solve-status')?.textContent,
  solveLabelLines: (() => {
    const range = document.createRange();
    range.selectNodeContents(document.querySelector('.solve-label'));
    return range.getClientRects().length;
  })(),
  solveWarning: document.querySelector('#solve-warning')?.textContent,
  solveSummary: document.querySelector('#solve-summary')?.textContent,
  inspectionSummary: document.querySelector('#inspection-summary')?.textContent,
  matesExpanded: document.querySelector('#mate-rows')?.closest('details')?.open ?? false,
  bomCount: document.querySelector('#bom-count')?.textContent,
  bomRows: [...document.querySelectorAll('.bom-row')].map(node => node.textContent),
  bomExpanded: document.querySelector('#bom-rows')?.closest('details')?.open ?? false,
  mateRows: document.querySelector('#mate-rows').closest('details').open
    ? [...document.querySelectorAll('.mate-row')].map(node => node.textContent) : [],
  occurrenceStates: [...document.querySelectorAll('.occurrence-state')].map(node => node.textContent),
  panelOverflowX: document.querySelector('#panel-scroll').scrollWidth > document.querySelector('#panel-scroll').clientWidth,
}));
const overlapPixels = async () => {
  await page.evaluate(() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve))));
  const bounds = await page.evaluate(() => window.coalesceViewer.overlapScreenBounds());
  const source = `data:image/png;base64,${(await page.screenshot()).toString('base64')}`;
  return page.evaluate(async ({ bounds, source }) => {
    const image = new Image();
    image.src = source;
    await image.decode();
    const sample = document.createElement('canvas');
    sample.width = image.naturalWidth;
    sample.height = image.naturalHeight;
    const context = sample.getContext('2d');
    context.drawImage(image, 0, 0);
    const pixels = context.getImageData(bounds.x, bounds.y, bounds.width, bounds.height).data;
    let dominant = 0;
    let pairTint = 0;
    for (let index = 0; index < pixels.length; index += 4) {
      const [red, green, blue] = pixels.slice(index, index + 3);
      if (red > 150 && green > 100 && red > blue * 1.5 && green > blue * 1.2) dominant += 1;
      if ((red > 120 && red > green * 1.3 && red > blue * 1.2)
          || (blue > 120 && blue > red * 1.2 && blue > green * 1.1)) pairTint += 1;
    }
    return { sampled: pixels.length / 4, dominant, pairTint, bounds };
  }, { bounds, source });
};
const initial = await state();
if (shots) await page.screenshot({ path: `${shots}/${width}-assembled-r3.png`, fullPage: true });
await page.locator('#mate-rows').evaluate(list => { list.closest('details').open = true; });
await page.locator('.mate-row').first().click();
const mateFocused = await state();
await page.keyboard.press('Escape');
const firstToggle = page.locator('.visibility').first();
await firstToggle.click();
const hidden = await state();
await firstToggle.click();
await page.locator('.assembly-row[data-status="ERROR"]').first().click();
const errorFocused = await state();
await page.keyboard.press('Escape');
const beforeFocusFrames = await page.evaluate(() => window.coalesceViewer.drawn.frames);
await page.locator('.assembly-row[data-status="FAIL"]').first().click();
await page.waitForFunction(frames => window.coalesceViewer.drawn.frames > frames, beforeFocusFrames);
const focused = await state();
focused.overlapPixels = await overlapPixels();
if (shots) await page.screenshot({ path: `${shots}/${width}-pair-focused-r3.png`, fullPage: true });
await page.locator('.assembly-more button').click();
const expanded = await state();
await page.locator('.assembly-row[data-status="PASS"]').nth(7).click();
const focusedPass = await state();
await page.locator('.assembly-more button').click();
const selectedCollapsed = await state();
await page.locator('.assembly-more button').click();
await writeFile(refreshTrigger, 'ready');
await page.waitForSelector('.fit-reason:has-text("refreshed-clearance")');
const refreshed = await state();
await page.keyboard.press('Escape');
const escaped = await state();
await page.locator('.assembly-more button').click();
const collapsed = await state();
await page.locator('.assembly-row').filter({ hasText: 'touching' }).click();
const touching = await state();
await page.keyboard.press('Escape');
await page.getByRole('button', { name: 'Section' }).click();
const section = await state();
console.log(JSON.stringify({ initial, mateFocused, hidden, errorFocused, focused, expanded, focusedPass, selectedCollapsed, refreshed, escaped, collapsed, touching, section, errors }));
await browser.close();
