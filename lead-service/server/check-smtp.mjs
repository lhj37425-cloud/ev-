import { verifySmtp } from './production.mjs';

try {
  await verifySmtp();
  console.log('SMTP connection and authentication verified. No message was sent.');
} catch (error) {
  console.error('SMTP verification failed:', error.code || error.name);
  process.exitCode = 1;
}
