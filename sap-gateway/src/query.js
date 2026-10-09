import { HANA_SCHEMA, exec } from './hana.js';

export const quoteIdentifier = (identifier) => `"${identifier}"`;

export const tableRef = (tableName) =>
  `${quoteIdentifier(HANA_SCHEMA)}.${quoteIdentifier(tableName)}`;

export const normalizeLimit = (limit, defaultValue = 20, max = 100) => {
  const value = limit ?? defaultValue;
  const parsed = Number.parseInt(value, 10);
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > max) {
    throw new Error(`limit must be an integer between 1 and ${max}.`);
  }
  return parsed;
};

export const optionalDocNum = (docNum) => {
  if (docNum === undefined || docNum === null || docNum === '') return null;
  const parsed = Number.parseInt(docNum, 10);
  if (!Number.isInteger(parsed) || parsed < 1) {
    throw new Error('doc_num must be a positive integer.');
  }
  return parsed;
};

export { exec };
