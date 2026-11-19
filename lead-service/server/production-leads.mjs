import { database } from './production.mjs';

const [action = 'list', arg] = process.argv.slice(2);
const db = database();
try {
  if (action === 'list') {
    const result = await db.query(`SELECT id, payload->>'name' AS name, payload->>'company' AS company,
      payload->>'contact' AS contact, payload->>'language' AS language, status, notification_status, created_at
      FROM leads ORDER BY created_at DESC LIMIT 50`);
    console.table(result.rows);
  } else if (action === 'failed') {
    const result = await db.query(`SELECT l.id, o.attempts, o.last_error, o.created_at
      FROM email_outbox o JOIN leads l USING (request_id) WHERE o.status='failed' ORDER BY o.created_at DESC`);
    console.table(result.rows);
  } else if (action === 'retry' && arg) {
    const result = await db.query(
      `UPDATE email_outbox SET status='pending', attempts=0,
      next_attempt_at=now(), locked_until=NULL, last_error=NULL WHERE request_id =
      (SELECT request_id FROM leads WHERE id=$1) AND status='failed' RETURNING id`,
      [arg]
    );
    if (!result.rowCount) throw new Error('Failed notification not found');
    await db.query("UPDATE leads SET notification_status='pending' WHERE id=$1", [arg]);
    console.log(`Queued retry for ${arg}`);
  } else {
    throw new Error('Usage: node server/production-leads.mjs list|failed|retry ED-ID');
  }
} catch (error) {
  console.error(error.message);
  process.exitCode = 1;
} finally {
  await db.end();
}
