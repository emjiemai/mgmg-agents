import hana from '@sap/hana-client';
import dotenv from 'dotenv';

dotenv.config();

const {
  HANA_SERVER,
  HANA_PORT = '30015',
  HANA_USER,
  HANA_PASSWORD,
  HANA_SCHEMA
} = process.env;

for (const [name, value] of Object.entries({
  HANA_SERVER,
  HANA_PORT,
  HANA_USER,
  HANA_PASSWORD,
  HANA_SCHEMA
})) {
  if (!value) throw new Error(`Missing required environment variable: ${name}`);
}

if (!/^\d+$/.test(String(HANA_PORT))) {
  throw new Error('HANA_PORT must be numeric.');
}

if (!/^[A-Za-z0-9_$#]+$/.test(HANA_SCHEMA)) {
  throw new Error('HANA_SCHEMA contains invalid identifier characters.');
}

const connection = hana.createConnection();

export const connect = () => new Promise((resolve, reject) => {
  connection.connect(
    {
      serverNode: `${HANA_SERVER}:${HANA_PORT}`,
      uid: HANA_USER,
      pwd: HANA_PASSWORD
    },
    (error) => error ? reject(error) : resolve()
  );
});

// `params` are bound to the SQL's `?` placeholders by the HANA client, so a
// value (a date, a code) is never pasted into the SQL text.
export const exec = (sql, params = []) => new Promise((resolve, reject) => {
  connection.exec(sql, params, (error, result) => error ? reject(error) : resolve(result));
});

export const disconnect = () => {
  try {
    connection.disconnect();
  } catch {
    // Ignore shutdown errors.
  }
};

export { HANA_SCHEMA };
