// GEO 页面验收：检查子页面无回归，并跑一遍问题库 → 估算 → 模拟体检 → 矩阵/报告链路。
// 用法：node tools/GEO验收.cjs
const { chromium } = require('D:/项目/node_modules/playwright');
const fs = require('fs');
const path = require('path');

const BASE = process.env.EV_BASE || 'http://127.0.0.1:8766';
const OUT = path.resolve(__dirname, '..', '验收-本地基线-2026-11-19', 'geo-runs');
let TOKEN = '';

const ROUTES = [
  ['GEO 总览', '/geo'],
  ['品牌档案', '/geo/brand'],
  ['品牌体检', '/geo/monitor'],
  ['问题库', '/geo/questions'],
  ['报告', '/geo/reports'],
  ['内容', '/geo/content'],
  ['发布', '/geo/publish'],
  ['自动执行', '/geo/auto'],
];

async function api(context, method, route, body) {
  const resp = await context.request.fetch(BASE + route, {
    method,
    headers: { Authorization: `Bearer ${TOKEN}`, 'Content-Type': 'application/json' },
    data: body === undefined ? undefined : JSON.stringify(body),
  });
  const text = await resp.text();
  let json = null;
  try { json = JSON.parse(text); } catch {}
  return { status: resp.status(), json, text };
}

async function shot(context, name, route) {
  const page = await context.newPage();
  const errors = [];
  page.on('console', msg => { if (msg.type() === 'error') errors.push('console: ' + msg.text().slice(0, 300)); });
  page.on('pageerror', err => errors.push('pageerror: ' + String(err).slice(0, 300)));
  let status = null;
  let info = {};
  try {
    const resp = await page.goto(BASE + route, { waitUntil: 'domcontentloaded', timeout: 60000 });
    status = resp ? resp.status() : null;
    await page.waitForTimeout(4000);
    info = await page.evaluate(() => ({
      title: document.title, url: location.href,
      body: document.body.innerText.slice(0, 1500),
    }));
    const file = path.join(OUT, name + '.png');
    await page.screenshot({ path: file, fullPage: true }).catch(() => {});
    console.log(`== ${name} ${route} status=${status} errors=${errors.length} ==`);
    console.log(String(info.body || '').replace(/\n+/g, ' | ').slice(0, 400));
    return { name, route, status, ...info, errors, screenshot: file };
  } catch (e) {
    console.log(`== ${name} ${route} ERROR == ${String(e).slice(0, 300)}`);
    return { name, route, status, error: String(e), errors };
  } finally {
    await page.close();
  }
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const profile = path.resolve(__dirname, '..', '.local', 'playwright-profile');
  const context = await chromium.launchPersistentContext(profile, { headless: true, viewport: { width: 1440, height: 1000 } });
  const results = [];
  const chain = {};

  // 1) 子页面扫描
  for (const [name, route] of ROUTES) {
    results.push(await shot(context, name, route));
  }

  // 2) 从页面 localStorage 取本地预览 token（不写死在脚本里）
  const tokenPage = await context.newPage();
  await tokenPage.goto(BASE + '/geo', { waitUntil: 'domcontentloaded', timeout: 60000 });
  await tokenPage.waitForTimeout(1000);
  TOKEN = await tokenPage.evaluate(() => localStorage.getItem('token') || '');
  await tokenPage.close();
  chain.token = { present: !!TOKEN };
  if (!TOKEN) {
    chain.error = '页面未注入本地预览 token，无法调用 GEO 接口';
    fs.writeFileSync(path.join(OUT, 'GEO验收.json'), JSON.stringify({ results, chain }, null, 2), 'utf8');
    await context.close();
    process.exit(1);
  }

  // 3) 找项目（优先 TestBrand）
  const projects = await api(context, 'GET', '/api/geo/projects');
  chain.projects = { status: projects.status, total: projects.json ? projects.json.total : null };
  let project = (projects.json?.items || []).find(item => item.name === 'TestBrand') || (projects.json?.items || [])[0];
  if (!project) {
    const created = await api(context, 'POST', '/api/geo/projects', {
      brand_name: 'TestBrand', aliases: ['TB'], website: 'https://example.com',
      industry: '露营', region: '', description: '', market: 'cn', locales: [],
    });
    project = created.json?.project;
    chain.created_project = { status: created.status, id: project?.id };
  }
  if (!project) {
    chain.error = '没有可用 GEO 项目';
    fs.writeFileSync(path.join(OUT, 'GEO验收.json'), JSON.stringify({ results, chain }, null, 2), 'utf8');
    await context.close();
    process.exit(1);
  }
  chain.project = { id: project.id, name: project.name };

  // 3) 问题库：不足 2 条则补 2 条
  const before = await api(context, 'GET', `/api/geo/projects/${project.id}/questions`);
  chain.questions_before = { status: before.status, total: before.json?.total };
  if ((before.json?.total || 0) < 2) {
    const added = await api(context, 'POST', `/api/geo/projects/${project.id}/questions`, {
      items: [
        { text: '露营帐篷怎么选？', intent: 'purchase', locale: 'zh' },
        { text: '露营装备哪个品牌好？', intent: 'compare', locale: 'zh' },
      ],
    });
    chain.questions_add = { status: added.status, added: added.json?.added, error: added.json?.error };
  }
  const after = await api(context, 'GET', `/api/geo/projects/${project.id}/questions`);
  chain.questions_after = { status: after.status, total: after.json?.total, active: after.json?.active };

  // 4) 估算
  const estimate = await api(context, 'POST', `/api/geo/projects/${project.id}/runs/estimate`, {
    engines: ['doubao', 'deepseek'], samples: 1,
  });
  chain.estimate = { status: estimate.status, estimate: estimate.json?.estimate };

  // 5) 启动模拟体检并轮询
  const started = await api(context, 'POST', `/api/geo/projects/${project.id}/runs`, {
    engines: ['doubao', 'deepseek'], samples: 1,
  });
  chain.run_start = { status: started.status, run: started.json?.run || started.json };
  const runId = started.json?.run?.id;
  if (runId) {
    const deadline = Date.now() + 240000;
    let run = null;
    while (Date.now() < deadline) {
      await new Promise(resolve => setTimeout(resolve, 2000));
      const current = await api(context, 'GET', `/api/geo/runs/${runId}`);
      run = current.json?.run;
      if (run && ['done', 'failed', 'cancelled'].includes(run.status)) break;
    }
    chain.run_final = run;
  }

  // 6) 矩阵与报告
  const matrix = await api(context, 'GET', `/api/geo/projects/${project.id}/matrix?run_id=${runId || ''}`);
  chain.matrix = {
    status: matrix.status,
    run_status: matrix.json?.run?.status,
    total: matrix.json?.total,
    engines: (matrix.json?.engines || []).map(engine => engine.name),
    sample_row: matrix.json?.rows?.[0] || null,
  };
  const report = await api(context, 'GET', `/api/geo/projects/${project.id}/reports?period=week`);
  chain.report = {
    status: report.status,
    runs: report.json?.report?.runs,
    answers: report.json?.report?.answers,
    questions: report.json?.report?.questions,
  };

  // 7) 跑完后回看关键页面
  for (const [name, route] of [['问题库-跑后', '/geo/questions'], ['体检-跑后', '/geo/monitor'], ['报告-跑后', '/geo/reports']]) {
    results.push(await shot(context, name, route));
  }

  fs.writeFileSync(path.join(OUT, 'GEO验收.json'), JSON.stringify({ results, chain }, null, 2), 'utf8');
  console.log('\n验收完成：' + path.join(OUT, 'GEO验收.json'));
  await context.close();
})().catch(e => { console.error(e); process.exit(1); });
