// 只读：打开 EV 指挥官各页面，记录标题、正文、控制台错误并截图。
// 用法：node tools/本地基线检查.cjs
const { chromium } = require('D:/项目/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.EV_BASE || 'http://127.0.0.1:8766';
const OUT = path.resolve(__dirname, '..', '验收-本地基线-2026-11-19');
const ROUTES = [
  ['首页', '/mb/home'],
  ['图片编辑', '/mb/production/image-edit'],
  ['获客总览', '/mb/acquisition'],
  ['获客线索', '/mb/acquisition/leads'],
  ['WhatsApp', '/mb/global/whatsapp'],
  ['设备农场/PC助理', '/mb/desktop-agent'],
  ['数字人', '/mb/production/digital-human'],
  ['充值', '/mb/recharge'],
  ['订单', '/mb/order'],
];

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const profile = path.resolve(__dirname, '..', '.local', 'playwright-profile');
  const context = await chromium.launchPersistentContext(profile, { headless: true, viewport: { width: 1440, height: 1000 } });
  const results = [];
  for (const [name, route] of ROUTES) {
    const page = await context.newPage();
    const errors = [];
    page.on('console', msg => { if (msg.type() === 'error') errors.push('console: ' + msg.text().slice(0, 300)); });
    page.on('pageerror', err => errors.push('pageerror: ' + String(err).slice(0, 300)));
    let status = null;
    try {
      const resp = await page.goto(BASE + route, { waitUntil: 'domcontentloaded', timeout: 60000 });
      status = resp ? resp.status() : null;
      await page.waitForTimeout(5000);
      const info = await page.evaluate(() => ({
        title: document.title,
        url: location.href,
        body: document.body.innerText.slice(0, 1200),
        hasToken: !!localStorage.getItem('token'),
      }));
      const file = path.join(OUT, name.replace(/[\\/:*?"<>|]/g, '_') + '.png');
      await page.screenshot({ path: file, fullPage: true }).catch(() => {});
      results.push({ name, route, status, ...info, errors, screenshot: file });
      console.log(`== ${name} ${route} status=${status} token=${info.hasToken} errors=${errors.length} ==`);
      console.log(info.body.replace(/\n+/g, ' | ').slice(0, 500));
    } catch (e) {
      results.push({ name, route, status, error: String(e), errors });
      console.log(`== ${name} ${route} ERROR == ${String(e).slice(0, 300)}`);
    }
    await page.close();
  }
  fs.writeFileSync(path.join(OUT, '基线检查.json'), JSON.stringify(results, null, 2), 'utf8');
  console.log('\n报告：' + path.join(OUT, '基线检查.json'));
  await context.close();
})().catch(e => { console.error(e); process.exit(1); });
