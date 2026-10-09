import crypto from 'node:crypto';
import dotenv from 'dotenv';

dotenv.config();

const configuredToken = process.env.API_TOKEN;

if (!configuredToken || configuredToken === 'CHANGE_ME_TO_A_LONG_RANDOM_TOKEN') {
  throw new Error('Set a strong API_TOKEN in .env before starting the server.');
}

export function requireApiToken(req, res, next) {
  const header = req.get('authorization') || '';
  const [scheme, token] = header.split(' ');

  if (scheme !== 'Bearer' || !token) {
    return res.status(401).json({ ok: false, error: 'Unauthorized' });
  }

  const a = Buffer.from(token);
  const b = Buffer.from(configuredToken);

  const valid = a.length === b.length && crypto.timingSafeEqual(a, b);

  if (!valid) {
    return res.status(401).json({ ok: false, error: 'Unauthorized' });
  }

  return next();
}
