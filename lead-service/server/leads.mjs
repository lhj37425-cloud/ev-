import { readFileSync, readdirSync, existsSync, writeFileSync, renameSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const dataDir = path.resolve(process.env.LEADS_DIR || path.join(root, '.local', 'leads'));
const records = existsSync(dataDir)
  ? readdirSync(dataDir)
      .filter((f) => f.endsWith('.json'))
      .map((file) => ({ file, data: JSON.parse(readFileSync(path.join(dataDir, file), 'utf8')) }))
  : [];
const args = process.argv.slice(2);
if (args[0] === 'status') {
  const [, id, status] = args;
  const allowed = ['new', 'contacted', 'scheduled', 'proposal', 'won', 'paused'];
  const record = records.find((r) => r.data.id === id);
  if (!record || !allowed.includes(status)) {
    console.error('Usage: npm run leads -- status ED-ID new|contacted|scheduled|proposal|won|paused');
    process.exit(1);
  }
  record.data.status = status;
  record.data.updatedAt = new Date().toISOString();
  const file = path.join(dataDir, record.file);
  writeFileSync(file + '.tmp', JSON.stringify(record.data, null, 2) + '\n', { mode: 0o600 });
  renameSync(file + '.tmp', file);
  console.log('Updated:', id, status);
} else if (args[0] === 'csv') {
  const keys = ['id', 'name', 'company', 'contact', 'service', 'message', 'source', 'language', 'status', 'createdAt'];
  const escape = (value) => {
    let text = String(value ?? '');
    if (/^[\s]*[=+@-]/.test(text)) text = "'" + text;
    return '"' + text.replaceAll('"', '""') + '"';
  };
  console.log(keys.join(','));
  for (const { data } of records) console.log(keys.map((k) => escape(data[k])).join(','));
} else {
  if (!records.length) console.log('暂无咨询记录。');
  else
    console.table(
      records.map(({ data }) => ({
        id: data.id,
        name: data.name,
        company: data.company,
        contact: data.contact,
        service: data.service,
        language: data.language,
        status: data.status,
        createdAt: data.createdAt,
      }))
    );
}
