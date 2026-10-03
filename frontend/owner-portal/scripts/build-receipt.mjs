import { createHash } from 'node:crypto';
import { execFileSync } from 'node:child_process';
import { readFileSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';

const root = path.resolve('../..');
const hash = (bytes) => createHash('sha256').update(bytes).digest('hex');
const assets = {};
function walk(directory, prefix = '') {
  for (const name of readdirSync(directory).sort()) {
    const full = path.join(directory, name);
    const relative = prefix + name;
    if (statSync(full).isDirectory()) walk(full, relative + '/');
    else {
      const bytes = readFileSync(full);
      assets[relative] = { sha256: hash(bytes), bytes: bytes.length };
    }
  }
}
walk('dist');
const receipt = {
  format: 'openstack-platform-owner-build-v1',
  sourceCommit: execFileSync('git', ['rev-parse', 'HEAD'], { cwd: root }).toString().trim(),
  dirty:
    execFileSync('git', ['status', '--porcelain', '--untracked-files=normal'], {
      cwd: root,
    }).toString().length !== 0,
  npmLockSha256: hash(readFileSync('../package-lock.json')),
  nodeVersion: process.versions.node,
  assetManifestSha256: assets['.vite/manifest.json'].sha256,
  assets,
};
writeFileSync('build-receipt.json', JSON.stringify(receipt, null, 2) + '\n');
console.log('owner-build-receipt=written');
