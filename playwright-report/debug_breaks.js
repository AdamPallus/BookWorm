const { chromium } = require('playwright');
(async () => {
  const BASE = 'http://localhost:8000';
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  
  await page.goto(`${BASE}/read/6`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  
  const result = await page.evaluate(async () => {
    await loadChapter(6, 0);
    const c = getReaderContainer();
    _pageBreaksKey = null;
    const t0 = performance.now();
    const breaks = buildPageBreaks(c);
    const t1 = performance.now();
    return {
      count: breaks.length,
      timeMs: Math.round(t1 - t0),
      first5: breaks.slice(0, 5),
      last5: breaks.slice(-5),
      scrollHeight: c.scrollHeight,
      clientHeight: c.clientHeight,
    };
  });
  console.log(JSON.stringify(result, null, 2));
  await browser.close();
})();
