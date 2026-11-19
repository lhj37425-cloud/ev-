import { readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import pg from 'pg';
import { createClient } from 'redis';
import nodemailer from 'nodemailer';

const { Pool } = pg;
const readSecret = (name) => {
  const file = process.env[`${name}_FILE`];
  return file ? readFileSync(file, 'utf8').trim() : process.env[name];
};
export const production = process.env.DEPLOY_MODE === 'production';
export const publicOrigin = production ? new URL(process.env.SITE_URL).origin : null;
export const publicHost = production ? new URL(process.env.SITE_URL).host : null;
const notificationTo = 'ezra.y.franklin@gmail.com';
let pool;
let redis;
let redisPromise;

export function database() {
  if (!pool) {
    pool = new Pool({
      host: process.env.PGHOST || 'postgres',
      port: Number(process.env.PGPORT || 5432),
      database: process.env.PGDATABASE || 'evol_dream',
      user: process.env.PGUSER || 'evol_dream',
      password: readSecret('PGPASSWORD'),
      max: 8,
      connectionTimeoutMillis: 5000,
      idleTimeoutMillis: 30000,
    });
    pool.on('error', (error) => console.error('PostgreSQL idle connection error:', error.code || error.name));
  }
  return pool;
}

export async function cache() {
  if (!redis) {
    redis = createClient({
      socket: {
        host: process.env.REDIS_HOST || 'redis',
        port: Number(process.env.REDIS_PORT || 6379),
        connectTimeout: 5000,
      },
      password: readSecret('REDIS_PASSWORD'),
    });
    redis.on('error', (error) => console.error('Redis connection error:', error.code || error.name));
  }
  if (!redis.isOpen) {
    if (!redisPromise)
      redisPromise = redis.connect().catch((error) => {
        redisPromise = null;
        throw error;
      });
    await redisPromise;
  }
  return redis;
}

export async function migrate() {
  await database().query(`
    CREATE TABLE IF NOT EXISTS leads (
      request_id uuid PRIMARY KEY,
      id text NOT NULL UNIQUE,
      fingerprint text NOT NULL,
      payload jsonb NOT NULL,
      status text NOT NULL DEFAULT 'new',
      notification_status text NOT NULL DEFAULT 'pending',
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE IF NOT EXISTS email_outbox (
      id bigserial PRIMARY KEY,
      request_id uuid NOT NULL UNIQUE REFERENCES leads(request_id) ON DELETE CASCADE,
      status text NOT NULL DEFAULT 'pending',
      attempts integer NOT NULL DEFAULT 0,
      next_attempt_at timestamptz NOT NULL DEFAULT now(),
      locked_until timestamptz,
      last_error text,
      sent_at timestamptz,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS email_outbox_due_idx ON email_outbox(next_attempt_at, id) WHERE status = 'pending';
  `);
}

export async function rateLimit(ip) {
  const key = `lead-rate:${createHash('sha256').update(ip).digest('hex')}`;
  const client = await cache();
  const results = await client.multi().incr(key).expire(key, 60).exec();
  return results[0] <= 12;
}

export async function ready() {
  await database().query('SELECT 1');
  await (await cache()).ping();
}

export async function saveProductionLead(input, lead, fingerprint, id) {
  const client = await database().connect();
  try {
    await client.query('BEGIN');
    const record = { ...lead, consent: true, preview: false, createdAt: new Date().toISOString() };
    const inserted = await client.query(
      `INSERT INTO leads (request_id, id, fingerprint, payload)
       VALUES ($1, $2, $3, $4) ON CONFLICT (request_id) DO NOTHING RETURNING id`,
      [input.requestId, id, fingerprint, record]
    );
    if (inserted.rowCount) {
      await client.query('INSERT INTO email_outbox (request_id) VALUES ($1)', [input.requestId]);
      await client.query('COMMIT');
      return { id, saved: true, notification: 'queued', status: 201 };
    }
    const existing = await client.query('SELECT id, fingerprint FROM leads WHERE request_id = $1', [input.requestId]);
    await client.query('COMMIT');
    if (existing.rows[0]?.fingerprint !== fingerprint) return { conflict: true, status: 409 };
    return { id: existing.rows[0].id, saved: true, duplicate: true, notification: 'queued', status: 200 };
  } catch (error) {
    await client.query('ROLLBACK').catch(() => {});
    throw error;
  } finally {
    client.release();
  }
}

function mailer() {
  const host = process.env.SMTP_HOST;
  const from = process.env.SMTP_FROM;
  if (!host || !from) throw new Error('SMTP_HOST and SMTP_FROM are required for the mail worker');
  const port = Number(process.env.SMTP_PORT || 587);
  const user = process.env.SMTP_USER;
  const password = readSecret('SMTP_PASSWORD');
  return nodemailer.createTransport(
    {
      host,
      port,
      secure: port === 465,
      requireTLS: port !== 465 && process.env.SMTP_ALLOW_PLAINTEXT !== 'true',
      auth: user ? { user, pass: password } : undefined,
      connectionTimeout: 10000,
      greetingTimeout: 10000,
      socketTimeout: 30000,
    },
    { from }
  );
}

export async function verifySmtp() {
  const transport = mailer();
  try {
    await transport.verify();
  } finally {
    transport.close();
  }
}

const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));
export async function runMailWorker() {
  const transport = mailer();
  await transport.verify();
  let running = true;
  process.on('SIGTERM', () => {
    running = false;
  });
  process.on('SIGINT', () => {
    running = false;
  });
  while (running) {
    try {
      const claimed = await database().query(`
        UPDATE email_outbox SET locked_until = now() + interval '90 seconds', attempts = attempts + 1
        WHERE id = (
          SELECT id FROM email_outbox
          WHERE status = 'pending' AND next_attempt_at <= now()
            AND (locked_until IS NULL OR locked_until < now())
          ORDER BY next_attempt_at, id FOR UPDATE SKIP LOCKED LIMIT 1
        ) RETURNING id, request_id, attempts
      `);
      const item = claimed.rows[0];
      if (!item) {
        await wait(3000);
        continue;
      }
      const result = await database().query('SELECT id, payload FROM leads WHERE request_id = $1', [item.request_id]);
      const lead = result.rows[0];
      const payload = lead.payload;
      const body = [
        `Lead ID: ${lead.id}`,
        `Time: ${payload.createdAt}`,
        `Language: ${payload.language}`,
        `Name: ${payload.name}`,
        `Company: ${payload.company}`,
        `Contact: ${payload.contact}`,
        `Service: ${payload.service}`,
        `Source: ${payload.source}`,
        `Message: ${payload.message || '(none)'}`,
        `Campaign: ${JSON.stringify(payload.campaign)}`,
      ].join('\n');
      try {
        await transport.sendMail({
          to: notificationTo,
          subject: `[EVOL DREAM] New inquiry ${lead.id}`,
          text: body,
        });
        const saved = await database().connect();
        try {
          await saved.query('BEGIN');
          await saved.query(
            "UPDATE email_outbox SET status='sent', sent_at=now(), locked_until=NULL, last_error=NULL WHERE id=$1",
            [item.id]
          );
          await saved.query("UPDATE leads SET notification_status='sent' WHERE request_id=$1", [item.request_id]);
          await saved.query('COMMIT');
        } catch (error) {
          await saved.query('ROLLBACK').catch(() => {});
          throw error;
        } finally {
          saved.release();
        }
        console.log(`Notification sent for ${lead.id}`);
      } catch (error) {
        const failed = item.attempts >= 8;
        const delay = Math.min(3600, 30 * 2 ** Math.min(item.attempts - 1, 7));
        await database().query(
          `UPDATE email_outbox SET status=$2, next_attempt_at=now()+($3 * interval '1 second'), locked_until=NULL,
           last_error=$4 WHERE id=$1`,
          [item.id, failed ? 'failed' : 'pending', delay, String(error.code || error.name).slice(0, 120)]
        );
        if (failed)
          await database().query("UPDATE leads SET notification_status='failed' WHERE request_id=$1", [
            item.request_id,
          ]);
        console.error(`Notification failed for ${lead.id}:`, error.code || error.name);
      }
    } catch (error) {
      console.error('Mail worker loop error:', error.code || error.name);
      await wait(5000);
    }
  }
  transport.close();
  await database().end();
}
