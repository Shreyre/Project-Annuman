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
  for (const view of ['transfers', 'care', 'national-view', 'validation', 'overview']) {
    await page.locator(`[data-view=${view}]`).click();
    await page.locator(`#${view}`).waitFor({ state: 'visible', timeout: 3000 }).catch(() => { throw new Error(`Navigation failed: ${view}`); });
  }
  await page.locator('[data-view=validation]').click();
  await page.locator('#real').waitFor({ state: 'visible', timeout: 5000 }).catch(() => { throw new Error('The real-data check is missing'); });
  check(await page.locator('#real tbody tr').count() === 2 && await page.locator('#real .chart').count() === 2, 'The real-data check lost a shortage');
  await page.locator('[data-view=care]').click();
  check(await page.locator('#careBoard .stat').count() === 4, 'Beds and staff board lost its summary');
  await page.locator('#careBoard summary').click();
  check(await page.locator('#careBoard .board tbody tr').count() === meta.facilities.length, 'Beds and staff board lost PHCs');
  await page.locator('[data-view=overview]').click();
  await page.locator('#help').click();
  await page.keyboard.press('Escape');
  check(!await page.locator('#guide').isVisible(), 'Dialog must close with Escape');
  await page.route('**/api/confirm*', route => route.fulfill({ status: 500, body: 'failed' }));
  await page.locator('[data-answer=empty]').click();
  await page.waitForFunction(() => document.querySelector('#confirmMsg').textContent.includes('Could not save'));
  check(await page.locator('#day').isEnabled(), 'Failed save must unlock controls');
  await page.unroute('**/api/confirm*');
  await page.evaluate(() => { navigator.mediaDevices.getUserMedia = async () => { throw new Error('Denied'); }; });
  await page.locator('.rec').click();
  await page.waitForFunction(() => document.querySelector('#confirmMsg').textContent.includes('Microphone unavailable'));
  check(await page.locator('#day').isEnabled(), 'Microphone denial must unlock controls');
  const originalDay = await page.locator('#dayOut').innerText();
  await page.route('**/api/day/125*', route => route.abort());
  await page.locator('#day').evaluate(el => { el.value = '125'; el.dispatchEvent(new Event('input', { bubbles: true })); });
  await page.waitForSelector('#loadError:not([hidden])');
  check(await page.locator('#dayOut').innerText() === originalDay, 'Failed day must not mislabel old data');
  await page.unroute('**/api/day/125*');
  await page.locator('#retry').click();
  await page.waitForSelector('#loadError', { state: 'hidden' });
  for (const width of [1440, 1024, 768, 390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Horizontal overflow at ${width}px`);
  }
  // the other networks: a scenario replay on real districts, and the live feed
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('#netSwitch [data-net=kerala]').click();
  await page.waitForFunction(() => document.querySelector('#networkSize').textContent.includes('14 districts'));
  await page.waitForSelector('#play:not([disabled])');     // the old network's panels stay up until the new one has loaded
  check(await page.locator('#context').isVisible(), 'A scenario must say what it is');
  check((await page.locator('.where').innerText()).includes('Kerala'), 'Scenario places must carry their real names');
  check(await page.locator('#stateFilter option').count() === 15, 'A one-state network filters by district');
  for (const width of [390, 320]) {
    await page.setViewportSize({ width, height: 900 });
    check(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `Horizontal overflow at ${width}px on the scenario`);
  }
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.locator('#netSwitch [data-net=live]').click();
  await page.waitForFunction(() => document.querySelector('#play span').textContent === 'Start feed');
  await page.waitForSelector('#play:not([disabled])');
  const liveDay = +(await page.locator('#dayMax').innerText());
  await page.locator('#play').click();
  await page.waitForFunction((d) => +document.querySelector('#dayMax').textContent >= d + 2, liveDay, { timeout: 20000 });
  await page.locator('#play').click();
  check((await page.locator('#context').innerText()).includes('PHCs have reported'), 'The live feed must report its progress');
  await page.locator('#netSwitch [data-net=demo]').click();
  await page.waitForFunction(() => document.querySelector('#networkSize').textContent.includes('2 states'));
  await page.waitForSelector('#play:not([disabled])');
  check(!await page.locator('#context').isVisible(), 'The demo network carries no scenario note');
  await page.emulateMedia({ colorScheme: 'dark', reducedMotion: 'reduce' });
  check(await page.evaluate(() => getComputedStyle(document.body).backgroundColor !== 'rgb(246, 247, 245)'), 'Dark theme not applied');
  check(errors.length === 0, `Browser exceptions: ${errors.join(', ')}`);
  await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('http://127.0.0.1:8788');
  await page.waitForSelector('.detail-tabs');
  return 'Passed: filters, pagination, matrix, investigation, navigation, the real-data check, beds and staff, ground truth, dialog, save errors, microphone fallback, request recovery, five viewport widths, the Kerala replay, the live feed, dark mode, reduced motion, and no browser exceptions.';
}
