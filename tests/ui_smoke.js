// Run with Playwright browser_run_code_unsafe(filename: "tests/ui_smoke.js").
// Uses the local demo; failed requests and microphone denial are simulated.
async (page) => {
  const check = (condition, message) => { if (!condition) throw new Error(message); };
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('http://127.0.0.1:8788');
  await page.waitForSelector('.detail-tabs');
  check(await page.locator('.signal-table tbody tr').count() === 8, 'Signal list must paginate');
  await page.locator('#search').fill('no-such-medicine');
  check(await page.locator('#clearFilters').isVisible(), 'Empty search must offer recovery');
  await page.locator('#clearFilters').click();
  await page.locator('#statusFilter').selectOption('phantom');
  check((await page.locator('.signal-table .badge').allTextContents()).every(t => t === 'Hidden stock-out'), 'Status filter leaked unrelated signals');
  await page.locator('#statusFilter').selectOption('');
  await page.locator('#nextPage').click();
  check((await page.locator('#pageNumber').innerText()).startsWith('2 /'), 'Next page failed');
  await page.locator('[data-display=matrix]').click();
  const meta = await (await page.request.get('http://127.0.0.1:8788/api/meta')).json();
  check(await page.locator('.cell').count() === meta.facilities.length * meta.drugs.length, 'Matrix lost facility/medicine cells');
  await page.locator('.cell').first().click();
  await page.waitForSelector('.detail-tabs');
  await page.locator('[data-display=list]').click();
  for (const tab of ['forecast', 'facility', 'evidence']) {
    await page.locator(`[data-detail-tab=${tab}]`).click();
    check(await page.locator(`[data-pane=${tab}]`).first().isVisible(), `Missing ${tab} panel`);
  }
  await page.locator('#truth').check();
  await page.waitForSelector('.truthline');
  await page.locator('#truth').uncheck();
  await page.waitForSelector('.truthline', { state: 'hidden' });
  for (const view of ['transfers', 'national-view', 'validation', 'overview']) {
    await page.locator(`[data-view=${view}]`).click();
    check(await page.locator(`#${view}`).isVisible(), `Navigation failed: ${view}`);
  }
  await page.locator('#help').click();
  await page.keyboard.press('Escape');
  check(!await page.locator('#guide').isVisible(), 'Dialog must close with Escape');
  await page.route('**/api/confirm', route => route.fulfill({ status: 500, body: 'failed' }));
  await page.locator('[data-answer=empty]').click();
  await page.waitForFunction(() => document.querySelector('#confirmMsg').textContent.includes('Could not save'));
  check(await page.locator('#day').isEnabled(), 'Failed save must unlock controls');
  await page.unroute('**/api/confirm');
  await page.evaluate(() => { navigator.mediaDevices.getUserMedia = async () => { throw new Error('Denied'); }; });
  await page.locator('.rec').click();
  await page.waitForFunction(() => document.querySelector('#confirmMsg').textContent.includes('Microphone unavailable'));
  check(await page.locator('#day').isEnabled(), 'Microphone denial must unlock controls');
  const originalDay = await page.locator('#dayOut').innerText();
  await page.route('**/api/day/125', route => route.abort());
  await page.locator('#day').evaluate(el => { el.value = '125'; el.dispatchEvent(new Event('input', { bubbles: true })); });
  await page.waitForSelector('#loadError:not([hidden])');
  check(await page.locator('#dayOut').innerText() === originalDay, 'Failed day must not mislabel old data');
  await page.unroute('**/api/day/125');
  await page.locator('#retry').click();
  await page.waitForSelector('#loadError', { state: 'hidden' });
  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Horizontal overflow at ${width}px`);
  }
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  check(await page.evaluate(() => getComputedStyle(document.body).backgroundColor !== 'rgb(246, 247, 245)'), 'Dark theme not applied');
  check(errors.length === 0, `Browser exceptions: ${errors.join(', ')}`);
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('http://127.0.0.1:8788');
  await page.waitForSelector('.detail-tabs');
  return 'Passed: filters, pagination, matrix, investigation, navigation, ground truth, dialog, save errors, microphone fallback, request recovery, five viewport widths, dark mode, reduced motion, and no browser exceptions.';
}
