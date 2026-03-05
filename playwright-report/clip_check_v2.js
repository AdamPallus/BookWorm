const { chromium } = require('playwright');
(async () => {
  const BASE = process.env.BOOKWORM_BASE_URL || 'http://localhost:8000';
  const TURNS = Number(process.env.PAGE_TURNS || 55);
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage({ viewport: { width: 1280, height: 900 } });
  await page.goto(`${BASE}/read/6`, { waitUntil: 'networkidle' });
  await page.waitForTimeout(2000);
  // Force chapter 6 load and cache invalidation
  await page.evaluate(async () => { _pageBreaksKey = null; await loadChapter(6, 0); afterPageChange(); });
  await page.waitForTimeout(1000);

  async function pageMetrics() {
    return await page.evaluate(() => {
      const container = document.querySelector('#readerSurface');
      const content = container && container.querySelector('.bw-chapter-content');
      if (!container || !content) return { error: 'missing' };
      const crect = container.getBoundingClientRect();
      const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
      let node, clippedBottom = 0, clippedTop = 0, visibleLines = 0;
      while ((node = walker.nextNode())) {
        if (!(node.textContent || '').trim()) continue;
        const range = document.createRange();
        range.selectNodeContents(node);
        for (const rect of range.getClientRects()) {
          if (rect.height <= 0) continue;
          if (rect.bottom <= crect.top || rect.top >= crect.bottom) continue;
          visibleLines++;
          if (rect.bottom > crect.bottom + 0.5) clippedBottom++;
          if (rect.top < crect.top - 0.5) clippedTop++;
        }
      }
      return { scrollTop: container.scrollTop, clientHeight: container.clientHeight, scrollHeight: container.scrollHeight, clippedBottom, clippedTop, visibleLines };
    });
  }

  let maxCB = 0, maxCT = 0, pagesWithClipping = 0;
  for (let i = 0; i <= TURNS; i++) {
    const m = await pageMetrics();
    if (m.clippedBottom > 0 || m.clippedTop > 0) pagesWithClipping++;
    maxCB = Math.max(maxCB, m.clippedBottom); maxCT = Math.max(maxCT, m.clippedTop);
    if (m.clippedBottom > 0 || m.clippedTop > 0) console.log(`page${i}`, JSON.stringify(m));
    if (i < TURNS) {
      await page.evaluate(async () => { await turnPage('next'); });
      await page.waitForTimeout(50);
    }
  }
  
  console.log('\n--- DETERMINISM TEST: back 10, then forward 10 ---');
  const beforeBack = await page.evaluate(() => document.querySelector('#readerSurface').scrollTop);
  for (let i = 0; i < 10; i++) { await page.evaluate(async () => { await turnPage('prev'); }); await page.waitForTimeout(50); }
  for (let i = 0; i < 10; i++) { await page.evaluate(async () => { await turnPage('next'); }); await page.waitForTimeout(50); }
  const afterRT = await page.evaluate(() => document.querySelector('#readerSurface').scrollTop);
  console.log(`Before: ${beforeBack}, After round-trip: ${afterRT}, Match: ${beforeBack === afterRT}`);
  
  // Also check break count
  const breakInfo = await page.evaluate(() => ({ count: _pageBreaks.length, first3: _pageBreaks.slice(0,3) }));
  console.log('Breaks:', JSON.stringify(breakInfo));

  console.log('\nSUMMARY', JSON.stringify({ maxClippedBottom: maxCB, maxClippedTop: maxCT, pagesWithClipping, turns: TURNS + 1 }));
  await browser.close();
})();
