// @vitest-environment node
import { describe, expect, it } from 'vitest';
import {
  admits,
  describeRequest,
  parseRange,
  requestSource,
  runtimeRequest,
  RuntimeVersionError,
  type Runtime,
} from './runtimeVersions';
import cases from './runtime-version-cases.json';

/** tests/test_runtime_versions.py runs the same cases through the build's parser. */
type RangeCase = { range: string; valid: boolean; admits?: string[]; rejects?: string[] };
type RequestCase = {
  name: string;
  runtime: Runtime;
  packageJson: Record<string, unknown>;
  files: Record<string, string>;
  request?: { source: string; label: string } | null;
  error?: string;
};

function version(text: string) {
  return text.split('.').map(Number) as [number, number, number];
}

describe('runtime version ranges', () => {
  it.each(cases.ranges as RangeCase[])('agrees with the build: "$range"', (item) => {
    const range = parseRange(item.range);
    if (!item.valid) {
      expect(range).toBeNull();
      return;
    }
    expect(range).not.toBeNull();
    for (const text of item.admits ?? []) expect(admits(range!, version(text))).toBe(true);
    for (const text of item.rejects ?? []) expect(admits(range!, version(text))).toBe(false);
  });
});

describe('runtime version requests', () => {
  it.each(cases.requests as RequestCase[])('agrees with the build: $name', async (item) => {
    const read = (name: string) => Promise.resolve(item.files[name] ?? null);
    const result = runtimeRequest(item.runtime, item.packageJson, read);
    if (item.error !== undefined) {
      await expect(result).rejects.toThrow(RuntimeVersionError);
      await expect(result).rejects.toThrow(item.error);
      return;
    }
    const request = await result;
    expect(request && { source: requestSource(request), label: describeRequest(request) }).toEqual(
      item.request,
    );
  });

  it('reads version files only when package.json asks for nothing', async () => {
    const read: string[] = [];
    const reader = (name: string) => {
      read.push(name);
      return Promise.resolve(null);
    };
    await runtimeRequest('node', { engines: { node: '22' } }, reader);
    await runtimeRequest('bun', { packageManager: 'bun@1.3.4' }, reader);
    expect(read).toEqual([]);
    await runtimeRequest('node', {}, reader);
    await runtimeRequest('bun', {}, reader);
    expect(read).toEqual(['.nvmrc', '.node-version', '.bun-version']);
  });
});
