const { chromium } = require('playwright');
(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto('http://localhost:8000/read/6', { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  await page.evaluate(async () => { await loadChapter(6, 0); afterPageChange(); });
  await page.waitForTimeout(500);
  for (let i = 0; i < 50; i++) {
    await page.evaluate(async () => { await turnPage('next'); });
    await page.waitForTimeout(30);
  }
  const info = await page.evaluate(() => {
    const c = document.querySelector('#readerSurface');
    const mask = c.parentElement.querySelector('.bw-page-mask');
    return { maskExists: !!mask, maskDisplay: mask?.style.display, maskHeight: mask?.style.height, maskBg: mask?.style.backgroundColor };
  });
  console.log('Mask info:', JSON.stringify(info));
  await page.screenshot({ path: '/Users/pallusa/.openclaw/media/page50_mask.png' });
  console.log('Done');
  await browser.close();
})();
