import { database, migrate } from './production.mjs';

try {
  await migrate();
  console.log('Database schema ready');
} catch (error) {
  console.error('Database migration failed:', error.code || error.name);
  process.exitCode = 1;
} finally {
  await database().end();
}
