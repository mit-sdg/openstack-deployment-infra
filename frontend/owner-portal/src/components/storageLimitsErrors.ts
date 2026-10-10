import type { StorageResource } from '../api';

const messages: Record<string, string> = {
  USAGE_NOT_FRESH:
    "There isn't a recent usage measurement to lower this limit safely. Wait for a new measurement, refresh the page, and try again.",
  MEMORY_BUDGET_EXCEEDED:
    "The storage host doesn't have enough memory left for this. Choose a smaller limit, lower another database's memory, or ask an operator to add capacity.",
  CONNECTION_BUDGET_EXCEEDED:
    "The storage host can't support that many database connections. Choose a smaller limit, lower another database's connection limit, or ask an operator to add capacity.",
  OBJECT_BUDGET_EXCEEDED:
    "The storage host can't support that many objects. Choose a smaller limit, lower another bucket's object limit, or ask an operator for help.",
  DISK_BUDGET_EXCEEDED:
    "The storage host doesn't have enough disk space left for this. Choose a smaller size or ask an operator to free space or add storage.",
  INSTANCE_MIGRATION_REQUIRED:
    'To increase connections, this database first needs to move to its own instance. Ask an operator to migrate it.',
};

function size(value: number, roundUp = false) {
  const divisor = value >= 1024 ** 3 ? 1024 ** 3 : 1024 ** 2;
  const rounded = (roundUp ? Math.ceil : Math.round)((value / divisor) * 10) / 10;
  return `${new Intl.NumberFormat('en-US').format(rounded)} ${divisor === 1024 ** 3 ? 'GB' : 'MB'}`;
}

export function storageLimitFailure(
  code: string | null | undefined,
  resource: StorageResource,
  retrying = false,
): string | null {
  if (code === 'INSTANCE_MANAGER_UNAVAILABLE')
    return retrying
      ? 'The storage service is temporarily unavailable. This change will retry automatically. If it does not finish, ask an operator to check storage.'
      : 'The storage service is temporarily unavailable. Wait a minute and try again, or ask an operator to check storage.';
  if (code !== 'SIZE_BELOW_USAGE')
    return code && Object.hasOwn(messages, code) ? messages[code] : null;
  const used = resource.usage.usedBytes;
  if (used === null || !Number.isSafeInteger(used) || used < 0)
    return 'That size is too small for the stored data and required database space. Refresh usage and choose a larger size.';
  const margin =
    resource.type === 'postgres'
      ? 384 * 1024 ** 2
      : resource.type === 'mongo'
        ? 128 * 1024 ** 2
        : 0;
  const minimum = Math.max(resource.type === 's3' ? 1024 ** 2 : 1024 ** 3, used + margin);
  const measurement = `Last measured usage was ${size(used)}.`;
  if (minimum > 500 * 1024 ** 3)
    return `${measurement} Delete some data before lowering the size; the largest allowed limit is 500 GB.`;
  return `${measurement} Choose at least ${size(minimum, true)}${margin ? ' to leave room for database overhead' : ''}.`;
}
