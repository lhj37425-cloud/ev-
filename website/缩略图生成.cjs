const fs = require('fs');
const path = require('path');
const {spawnSync} = require('child_process');
const frontData = require('./前台数据.cjs');
const admin = require('./后台服务.cjs');

const dataRoot = path.resolve(process.env.ADMIN_DATA_DIR || path.join(__dirname, 'data', 'admin-backend'));
const cacheDir = path.join(dataRoot, 'thumbnail-cache');
fs.mkdirSync(cacheDir, {recursive: true});
const covers = new Set();
const qualifications = new Set();
function addUrl(set, url) {
  const match = url?.match(/^\/admin-media\/([^/?]+)$/);
  if (match) set.add(decodeURIComponent(match[1]));
}
for (const lang of ['zh', 'vi', 'en']) {
  for (const item of frontData.news(lang)) addUrl(covers, item.cover);
  for (const item of frontData.qualifications(lang)) addUrl(qualifications, item[2]);
}

function generate(id, variant) {
  const original = admin.publicFile(id);
  if (!original || !/\.(?:jpe?g|png|webp)$/i.test(original)) return;
  const stat = fs.statSync(original);
  const target = path.join(cacheDir, id + (variant === 'zoom' ? '.zoom.webp' : '.webp'));
  const metadata = target + '.json';
  try {
    const previous = JSON.parse(fs.readFileSync(metadata, 'utf8'));
    if (previous.size === stat.size && previous.mtimeMs === stat.mtimeMs && fs.statSync(target).isFile()) return;
  } catch {}
  const temp = target + '.' + process.pid + '.tmp';
  const options = variant === 'zoom' ? ['-q', '90'] : ['-q', '80', '-resize', '960', '0'];
  const convert = spawnSync('cwebp', ['-quiet', ...options, original, '-o', temp], {encoding: 'utf8'});
  if (convert.error || convert.status !== 0) {
    if (fs.existsSync(temp)) fs.unlinkSync(temp);
    throw new Error('Image conversion failed: ' + (convert.error?.message || convert.stderr));
  }
  if (fs.statSync(temp).size >= stat.size) {
    fs.unlinkSync(temp);
    console.log('Original is smaller:', id, variant);
    return;
  }
  const after = fs.statSync(original);
  if (after.size !== stat.size || after.mtimeMs !== stat.mtimeMs) {
    fs.unlinkSync(temp);
    throw new Error('Original changed during conversion: ' + id);
  }
  fs.renameSync(temp, target);
  const metaTemp = metadata + '.' + process.pid + '.tmp';
  fs.writeFileSync(metaTemp, JSON.stringify({size: stat.size, mtimeMs: stat.mtimeMs}));
  fs.renameSync(metaTemp, metadata);
  console.log('Image ready:', id, variant);
}

for (const id of new Set([...covers, ...qualifications])) generate(id, 'thumb');
for (const id of qualifications) generate(id, 'zoom');
