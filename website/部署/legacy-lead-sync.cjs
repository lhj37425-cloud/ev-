// Import leads accepted by the existing PostgreSQL/email service into the new CMS.
// The public form keeps using that service during the migration, so mail delivery
// and idempotency stay intact. Only IDs are kept in the sync cursor.
const fs = require('node:fs');
const path = require('node:path');
const { execFileSync } = require('node:child_process');
const { withLock } = require('../文件锁.cjs');

const dataDir = process.env.MVP_DATA_DIR || '/srv/evol/data';
const leadFile = path.join(dataDir, 'mvp-leads.json');
const seenFile = path.join(dataDir, 'legacy-sync-seen.json');
const container = process.env.LEGACY_DB_CONTAINER || 'evol-dream-postgres-1';
const sql = "SELECT json_build_object('id', id, 'requestId', request_id::text, " +
  "'createdAt', created_at, 'payload', payload)::text " +
  'FROM leads ORDER BY created_at ASC, id ASC';

function readJson(file, fallback) {
  return fs.existsSync(file) ? JSON.parse(fs.readFileSync(file, 'utf8')) : fallback;
}

function writeAtomic(file, value, owner) {
  const temp = `${file}.${process.pid}.tmp`;
  try {
    fs.writeFileSync(temp, JSON.stringify(value, null, 2), { mode: owner ? 0o640 : 0o600 });
    if (owner) fs.chownSync(temp, owner.uid, owner.gid);
    fs.renameSync(temp, file);
  } finally {
    if (fs.existsSync(temp)) fs.unlinkSync(temp);
  }
}

const output = execFileSync('/usr/bin/docker', [
  'exec', container, 'psql', '-U', 'evol_dream', '-d', 'evol_dream',
  '-A', '-t', '-c', sql,
], { encoding: 'utf8', maxBuffer: 16 * 1024 * 1024 });
const source = output.trim() ? output.trim().split('\n').map(line => JSON.parse(line)) : [];
for (const row of source) {
  if (typeof row.id !== 'string' || !row.payload || typeof row.payload !== 'object') {
    throw new Error('Unexpected legacy lead row');
  }
}

withLock(`${leadFile}.lock`, () => {
  const leads = readJson(leadFile, []);
  const previouslySeen = readJson(seenFile, []);
  if (!Array.isArray(leads) || !Array.isArray(previouslySeen)) {
    throw new Error('Lead data or sync cursor is not an array');
  }
  const seen = new Set(previouslySeen);
  const present = new Set(leads.map(item => item.id));
  let imported = 0;
  for (const row of source) {
    if (seen.has(row.id)) continue;
    if (!present.has(row.id)) {
      const payload = row.payload;
      leads.push({
        id: row.id,
        requestId: row.requestId,
        name: payload.name || '',
        company: payload.company || '',
        contact: payload.contact || '',
        service: payload.service || '',
        message: payload.message || '',
        consent: payload.consent === true,
        language: payload.language || 'zh',
        source: payload.source || '',
        campaign: payload.campaign || {},
        createdAt: payload.createdAt || row.createdAt,
      });
      present.add(row.id);
      imported++;
    }
    seen.add(row.id);
  }
  if (imported) {
    leads.sort((a, b) => String(b.createdAt).localeCompare(String(a.createdAt)));
    writeAtomic(leadFile, leads, fs.statSync(leadFile));
  }
  if (seen.size !== previouslySeen.length) writeAtomic(seenFile, [...seen], fs.statSync(leadFile));
  if (imported) console.log(`Imported ${imported} legacy lead(s) into the CMS`);
});
