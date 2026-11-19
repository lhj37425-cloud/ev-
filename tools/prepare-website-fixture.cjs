const fs = require('node:fs');
const path = require('node:path');

const root = path.resolve(__dirname, '..');
const fixture = path.join(root, 'fixtures', 'website-admin');
const runtime = path.join(root, 'website', 'data', 'admin-backend');

if (fs.existsSync(runtime)) {
  console.error('本地官网数据已存在，保留原数据。');
  process.exit(1);
}

const staging = path.join(path.dirname(runtime), '.website-fixture-' + process.pid);
fs.mkdirSync(path.dirname(runtime), { recursive: true });
try {
  fs.cpSync(fixture, staging, { recursive: true });
  for (const [folder, suffixes] of [
    ['thumbnail-cache', ['.zoom.webp.json', '.webp.json']],
    ['video-cache', ['.json']],
  ]) {
    const cache = path.join(staging, folder);
    if (!fs.existsSync(cache)) continue;
    for (const name of fs.readdirSync(cache)) {
      const suffix = suffixes.find((item) => name.endsWith(item));
      if (!suffix) continue;
      const original = path.join(staging, 'uploads', name.slice(0, -suffix.length));
      const metaPath = path.join(cache, name);
      if (!fs.existsSync(original)) continue;
      const metadata = JSON.parse(fs.readFileSync(metaPath, 'utf8'));
      const originalStat = fs.statSync(original);
      if (metadata.size === originalStat.size) {
        metadata.mtimeMs = originalStat.mtimeMs;
        fs.writeFileSync(metaPath, JSON.stringify(metadata) + '\n');
      }
    }
  }
  fs.renameSync(staging, runtime);
} finally {
  fs.rmSync(staging, { recursive: true, force: true });
}
console.log('官网的已发布内容和公开媒体已初始化。');
