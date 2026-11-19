import messages from '../src/i18n/messages.json' with { type: 'json' };
import http from 'node:http';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { createReadStream, existsSync, statSync, mkdirSync, readFileSync, writeFileSync, renameSync } from 'node:fs';
import { randomUUID, createHash } from 'node:crypto';
import { isIP } from 'node:net';
import { production, publicHost, publicOrigin, rateLimit, ready, saveProductionLead } from './production.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dist = path.join(root, 'dist');
const dataDir = path.resolve(process.env.LEADS_DIR || path.join(root, '.local', 'leads'));
const port = Number(process.env.PORT || 4321);
const serviceAliases = {
  AI培训: 'training',
  Agent部署: 'agents',
  项目实施: 'implementation',
  合作交流: 'partnerships',
};
const acceptedServices = new Set(Object.values(serviceAliases));
const rates = new Map();
const mime = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.webp': 'image/webp',
  '.avif': 'image/avif',
  '.xml': 'application/xml',
  '.txt': 'text/plain; charset=utf-8',
  '.woff2': 'font/woff2',
  '.json': 'application/json',
};
const headers = {
  'X-Content-Type-Options': 'nosniff',
  'X-Frame-Options': 'DENY',
  'Referrer-Policy': 'strict-origin-when-cross-origin',
  'Permissions-Policy': 'camera=(), microphone=(), geolocation=()',
};
function json(res, status, payload) {
  res.writeHead(status, { ...headers, 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(JSON.stringify(payload));
}
function fail(status, message) {
  const error = new Error(message);
  error.status = status;
  throw error;
}
function normalize(data) {
  if (!data || typeof data !== 'object' || Array.isArray(data)) fail(400, '提交内容格式不正确。');
  if (data.website) fail(400, '提交未通过验证，请重试。');
  const result = {};
  for (const [key, max, required] of [
    ['name', 60, true],
    ['company', 120, true],
    ['contact', 120, true],
    ['message', 2000, false],
  ]) {
    if (data[key] !== undefined && typeof data[key] !== 'string') fail(400, '请检查表单字段格式。');
    const value = (data[key] || '').trim();
    if ((required && !value) || value.length > max) fail(400, '请填写姓名、公司与联系方式，并检查内容长度。');
    result[key] = value;
  }
  const email = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
  const phone = /^\+?[\d\s().-]{7,30}$/;
  if (!email.test(result.contact) && !(phone.test(result.contact) && result.contact.replace(/\D/g, '').length >= 7))
    fail(400, '请输入有效邮箱或带区号的电话号码。');
  const service = serviceAliases[data.service] || data.service;
  if (!acceptedServices.has(service)) fail(400, '请选择意向服务。');
  if (data.consent !== true) fail(400, '请阅读并同意隐私政策。');
  if (
    typeof data.requestId !== 'string' ||
    !/^[a-f0-9]{8}-[a-f0-9]{4}-[1-8][a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/i.test(data.requestId)
  )
    fail(400, '提交标识无效，请刷新后重试。');
  result.service = service;
  const language = data.language ?? 'zh';
  if (!['zh', 'vi', 'en'].includes(language)) fail(400, '请选择支持的语言。');
  result.language = language;
  result.source =
    typeof data.source === 'string' && new RegExp(`^/${language}/[a-z0-9/-]*$`).test(data.source)
      ? data.source.slice(0, 240)
      : `/${language}/`;
  result.campaign = {};
  for (const key of ['utm_source', 'utm_medium', 'utm_campaign']) {
    if (typeof data.campaign?.[key] === 'string') result.campaign[key] = data.campaign[key].slice(0, 120);
  }
  result.consentVersion = production ? 'production-2026-10-14' : 'local-preview-2026-10-14';
  return result;
}
async function saveLead(req, res) {
  let language = ['zh', 'vi', 'en'].includes(req.headers['x-site-language']) ? req.headers['x-site-language'] : 'zh';
  const reply = (status, payload) => {
    if (payload.error && language !== 'zh')
      payload.error = messages[payload.error]?.[language] || messages['暂时无法提交，请稍后重试。'][language];
    return json(res, status, payload);
  };
  if (req.method !== 'POST') return reply(405, { error: '请使用表单提交咨询。' });
  const expectedOrigin = production ? publicOrigin : `http://${req.headers.host}`;
  if (req.headers.origin !== expectedOrigin) return reply(403, { error: '提交来源不正确，请从本站表单重新提交。' });
  if (!req.headers['content-type']?.startsWith('application/json'))
    return reply(415, { error: '请使用网站表单提交。' });
  const forwarded = req.headers['x-real-ip'];
  const ip = production && typeof forwarded === 'string' && isIP(forwarded) ? forwarded : req.socket.remoteAddress;
  try {
    if (production) {
      if (!(await rateLimit(ip))) return reply(429, { error: '提交较频繁，请稍后再试。' });
    } else {
      const now = Date.now();
      const recent = (rates.get(ip) || []).filter((t) => now - t < 60000);
      if (recent.length >= 12) return reply(429, { error: '提交较频繁，请稍后再试。' });
      recent.push(now);
      rates.set(ip, recent);
    }
    const chunks = [];
    let size = 0;
    for await (const chunk of req) {
      size += chunk.length;
      if (size > 16384) fail(413, '提交内容过长，请缩短需求描述。');
      chunks.push(chunk);
    }
    const body = Buffer.concat(chunks).toString('utf8');
    let input;
    try {
      input = JSON.parse(body);
    } catch {
      fail(400, '提交内容格式不正确。');
    }
    if (['zh', 'vi', 'en'].includes(input?.language)) language = input.language;
    const lead = normalize(input);
    const fingerprint = createHash('sha256').update(JSON.stringify(lead)).digest('hex');
    const id = `ED-${new Date().toISOString().slice(0, 10).replaceAll('-', '')}-${randomUUID().slice(0, 8).toUpperCase()}`;
    if (production) {
      const result = await saveProductionLead(input, lead, fingerprint, id);
      if (result.conflict) fail(409, '同一次提交内容已发生变化，请刷新页面后重新提交。');
      return reply(result.status, {
        id: result.id,
        saved: true,
        duplicate: !!result.duplicate,
        notification: result.notification,
      });
    }
    mkdirSync(dataDir, { recursive: true, mode: 0o700 });
    const file = path.join(dataDir, `${input.requestId}.json`);
    if (existsSync(file)) {
      const existing = JSON.parse(readFileSync(file, 'utf8'));
      if (existing.fingerprint !== fingerprint) fail(409, '同一次提交内容已发生变化，请刷新页面后重新提交。');
      return reply(200, { id: existing.id, saved: true, duplicate: true });
    }
    const record = {
      id,
      requestId: input.requestId,
      ...lead,
      consent: true,
      fingerprint,
      status: 'new',
      createdAt: new Date().toISOString(),
      notification: 'not-configured',
      preview: true,
    };
    const temp = path.join(dataDir, `.${input.requestId}.tmp`);
    writeFileSync(temp, JSON.stringify(record, null, 2) + '\n', { flag: 'wx', mode: 0o600, flush: true });
    renameSync(temp, file);
    return reply(201, { id, saved: true });
  } catch (error) {
    if (!error.status) console.error('Lead storage failed:', error.code || error.name);
    return reply(error.status || 503, {
      error: error.status ? error.message : '暂时无法保存咨询，请保留内容并稍后重试。',
    });
  }
}
const server = http.createServer(async (req, res) => {
  const actualPort = server.address().port;
  const allowedHosts = production ? [publicHost] : [`127.0.0.1:${actualPort}`, `localhost:${actualPort}`];
  if (!allowedHosts.includes(req.headers.host))
    return json(res, 403, { error: 'This preview is available on localhost only.' });
  let pathname;
  try {
    pathname = decodeURIComponent(new URL(req.url, `http://${req.headers.host}`).pathname);
  } catch {
    return json(res, 400, { error: 'Invalid path' });
  }
  if (pathname === '/api/leads') return saveLead(req, res);
  if (production && pathname === '/readyz') {
    try {
      await ready();
      return json(res, 200, { ready: true });
    } catch {
      return json(res, 503, { ready: false });
    }
  }
  if (!['GET', 'HEAD'].includes(req.method)) return json(res, 405, { error: 'Method not allowed' });
  if (pathname === '/') {
    res.writeHead(302, { ...headers, Location: '/vi/' });
    return res.end();
  }
  if (pathname.includes('\0') || pathname.split('/').some((part) => part.startsWith('.')))
    return json(res, 404, { error: 'Not found' });
  let file = path.resolve(dist, '.' + pathname);
  if (!file.startsWith(dist + path.sep)) return json(res, 404, { error: 'Not found' });
  let status = 200;
  if (existsSync(file) && statSync(file).isDirectory()) {
    if (!pathname.endsWith('/')) {
      res.writeHead(308, {
        ...headers,
        Location: pathname + '/' + new URL(req.url, `http://${req.headers.host}`).search,
      });
      return res.end();
    }
    file = path.join(file, 'index.html');
  }
  if (!existsSync(file) || !statSync(file).isFile()) {
    status = 404;
    const language = pathname.split('/')[1];
    const localized404 = path.join(dist, ['zh', 'vi', 'en'].includes(language) ? language : 'zh', '404', 'index.html');
    file = existsSync(localized404) ? localized404 : path.join(dist, '404.html');
  }
  if (!existsSync(file)) return json(res, 503, { error: 'Build the website with npm run build first.' });
  res.writeHead(status, {
    ...headers,
    'Content-Type': mime[path.extname(file)] || 'application/octet-stream',
    'Cache-Control': pathname.startsWith('/_astro/') ? 'public, max-age=31536000, immutable' : 'no-cache',
  });
  if (req.method === 'HEAD') return res.end();
  createReadStream(file).pipe(res);
});
server.listen(port, production ? '0.0.0.0' : '127.0.0.1', () =>
  console.log(
    `EVOL DREAM ${production ? 'production API' : 'local preview'}: http://127.0.0.1:${server.address().port}/vi/`
  )
);
