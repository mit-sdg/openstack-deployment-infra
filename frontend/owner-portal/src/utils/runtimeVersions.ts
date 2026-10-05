/**
 * The Node.js or Bun version a commit asks for, read the way the build reads
 * it (openstack_platform/runtime_versions.py): engines.node, .nvmrc or
 * .node-version for Node.js; "packageManager": "bun@x.y.z", engines.bun or
 * .bun-version for Bun. Ranges use the same subset of npm's semver syntax.
 * runtime-version-cases.json runs through both parsers. Only the build can
 * look up releases, so the browser says what is asked, not what it resolves to.
 */
export type Runtime = 'node' | 'bun';

export const runtimeNames: Record<Runtime, string> = { node: 'Node.js', bun: 'Bun' };
/** The oldest release line a request may name, as [major, minor]. */
const OLDEST_LINES: Record<Runtime, [number, number]> = { node: [20, 0], bun: [1, 1] };
export const VERSION_FILES: Record<Runtime, string[]> = {
  node: ['.nvmrc', '.node-version'],
  bun: ['.bun-version'],
};
export const VERSION_FILE_BYTES = 1_024;
const MAXIMUM_REQUEST_LENGTH = 256;
const EXAMPLES: Record<Runtime, string> = { node: '>=22', bun: '^1.3' };

/** A request the build can't honour; the message is the build's own. */
export class RuntimeVersionError extends Error {}

/** [major, minor, patch, release]: release is 0 for a pre-release, 1 otherwise. */
type Key = [number, number, number, number];
type Bound = { key: Key; inclusive: boolean };
/** One comparator set: every npm set is a single interval of versions. */
type Interval = { low: Bound | null; high: Bound | null };
export type VersionRange = Interval[];

/** Larger than any accepted component, which has at most nine digits. */
const UNBOUNDED = 1_000_000_000;
const NUMBER = '0|[1-9][0-9]{0,8}';
const PART = `${NUMBER}|[xX*]`;
const IDENTIFIERS = '[0-9A-Za-z-]+(?:\\.[0-9A-Za-z-]+)*';
const PARTIAL = new RegExp(
  `^v?(${PART})(?:\\.(${PART})(?:\\.(${PART})(-${IDENTIFIERS})?(?:\\+${IDENTIFIERS})?)?)?$`,
);
const EXACT = new RegExp(`^(${NUMBER})\\.(${NUMBER})\\.(${NUMBER})$`);
const COMPARATOR = /^(\^|~>?|>=|<=|>|<|=)?(.+)$/;
const OPERATOR_SPACE = /(\^|~>?|[<>]=?|=) +/g;
const HYPHEN = /^([^ ]+) - ([^ ]+)$/;
const RANGE_CHARACTERS = /^[0-9A-Za-z.+*~^<>=| -]*$/;
const ALIAS = /^[A-Za-z][A-Za-z0-9/*_.-]*$/;
const QUOTABLE = /^[ !#-[\]-~]{1,64}$/;

function compare(a: Key, b: Key) {
  for (let index = 0; index < 4; index++) if (a[index] !== b[index]) return a[index] - b[index];
  return 0;
}

function admitsKey({ low, high }: Interval, key: Key) {
  const above =
    low === null || compare(key, low.key) > 0 || (low.inclusive && !compare(key, low.key));
  const below =
    high === null || compare(key, high.key) < 0 || (high.inclusive && !compare(key, high.key));
  return above && below;
}

function intersect(a: Interval, b: Interval): Interval {
  // At an equal key, an exclusive bound is the tighter one.
  const lows = [a.low, b.low].filter((bound): bound is Bound => bound !== null);
  const highs = [a.high, b.high].filter((bound): bound is Bound => bound !== null);
  const tighter = (rank: (x: Bound, y: Bound) => number) => (bounds: Bound[]) =>
    bounds.length ? bounds.reduce((best, item) => (rank(item, best) > 0 ? item : best)) : null;
  return {
    low: tighter((x, y) => compare(x.key, y.key) || Number(!x.inclusive) - Number(!y.inclusive))(
      lows,
    ),
    high: tighter((x, y) => compare(y.key, x.key) || Number(y.inclusive) - Number(x.inclusive))(
      highs,
    ),
  };
}

/** The lowest release in an interval, if it has any. */
function lowest(interval: Interval): Key | null {
  let key: Key = [0, 0, 0, 1];
  if (interval.low) {
    const [major, minor, patch, release] = interval.low.key;
    key = [major, minor, patch + (release && !interval.low.inclusive ? 1 : 0), 1];
  }
  return admitsKey(interval, key) ? key : null;
}

/** The highest release in a non-empty interval; parts may be unbounded. */
function highest({ high }: Interval): Key {
  if (!high) return [UNBOUNDED, UNBOUNDED, UNBOUNDED, 1];
  const [major, minor, patch, release] = high.key;
  if (high.inclusive && release) return [major, minor, patch, 1];
  if (patch) return [major, minor, patch - 1, 1];
  if (minor) return [major, minor - 1, UNBOUNDED, 1];
  return [major - 1, UNBOUNDED, UNBOUNDED, 1];
}

const ANY: Interval = { low: null, high: null };
const NOTHING: Interval = { low: null, high: { key: [0, 0, 0, 0], inclusive: false } };

export function admits(range: VersionRange, version: [number, number, number]) {
  return range.some((interval) => admitsKey(interval, [...version, 1]));
}

/** An exact release version such as 22.11.0, without a prefix or tag. */
export function parseVersion(value: string): [number, number, number] | null {
  const match = EXACT.exec(value);
  return match ? [Number(match[1]), Number(match[2]), Number(match[3])] : null;
}

/** Leading numeric parts and whether a pre-release tag follows them. */
function partial(value: string): [number[], boolean] | null {
  const match = PARTIAL.exec(value);
  if (!match) return null;
  const parts: number[] = [];
  let wildcard = false;
  for (const item of [match[1], match[2], match[3]]) {
    if (item === undefined) break;
    if (['x', 'X', '*'].includes(item)) wildcard = true;
    else if (wildcard)
      return null; // 1.x.3 names no version range.
    else parts.push(Number(item));
  }
  const prerelease = match[4] !== undefined;
  if (prerelease && parts.length < 3) return null;
  return [parts, prerelease];
}

/** The first version a partial names: 1.2 is 1.2.0-0, 1.2.3 is itself. */
function start(parts: number[], prerelease: boolean): Key {
  if (parts.length === 3) return [parts[0], parts[1], parts[2], prerelease ? 0 : 1];
  return [parts[0], parts[1] ?? 0, 0, 0];
}

/** The first version after a partial: 1 is 2.0.0-0, 1.2 is 1.3.0-0. */
function after(parts: number[]): Key {
  if (parts.length === 1) return [parts[0] + 1, 0, 0, 0];
  if (parts.length === 2) return [parts[0], parts[1] + 1, 0, 0];
  return [parts[0], parts[1], parts[2] + 1, 0];
}

function comparator(token: string): Interval | null {
  const match = COMPARATOR.exec(token);
  const parsed = match ? partial(match[2]) : null;
  if (!match || !parsed) return null;
  const operator = match[1] ?? '=';
  const [parts, prerelease] = parsed;
  if (!parts.length) return ['<', '>'].includes(operator) ? NOTHING : ANY;
  const first = start(parts, prerelease);
  const exact = parts.length === 3;
  const major = parts[0];
  const bound = (key: Key, inclusive: boolean): Bound => ({ key, inclusive });
  switch (operator) {
    case '=':
      return exact
        ? { low: bound(first, true), high: bound(first, true) }
        : { low: bound(first, true), high: bound(after(parts), false) };
    case '>=':
      return { low: bound(first, true), high: null };
    case '>':
      return { low: exact ? bound(first, false) : bound(after(parts), true), high: null };
    case '<':
      return { low: null, high: bound(first, false) };
    case '<=':
      return { low: null, high: exact ? bound(first, true) : bound(after(parts), false) };
    case '~':
    case '~>':
      return { low: bound(first, true), high: bound(after(parts.slice(0, 2)), false) };
  }
  // Caret: the first nonzero part, or the last given one, may not change.
  let high: Key;
  if (major || parts.length === 1) high = [major + 1, 0, 0, 0];
  else if (parts[1] || parts.length === 2) high = [0, parts[1] + 1, 0, 0];
  else high = [0, 0, parts[2] + 1, 0];
  return { low: bound(first, true), high: bound(high, false) };
}

function hyphen(first: string, last: string): Interval | null {
  const low = partial(first);
  const high = partial(last);
  if (!low || !high) return null;
  return {
    low: low[0].length ? { key: start(...low), inclusive: true } : null,
    high: !high[0].length
      ? null
      : high[0].length === 3
        ? { key: start(...high), inclusive: true }
        : { key: after(high[0]), inclusive: false },
  };
}

/** Parse the supported subset of npm's range syntax; null if it isn't one. */
export function parseRange(value: string): VersionRange | null {
  if (value.length > MAXIMUM_REQUEST_LENGTH || !RANGE_CHARACTERS.test(value)) return null;
  const alternatives: Interval[] = [];
  for (const alternative of value.split('||')) {
    const words = alternative
      .split(' ')
      .filter((word) => word)
      .join(' ');
    const range = HYPHEN.exec(words);
    let interval: Interval | null = ANY;
    if (range) interval = hyphen(range[1], range[2]);
    else
      for (const token of words.replace(OPERATOR_SPACE, '$1').split(' ')) {
        if (!token) continue;
        const item = comparator(token);
        if (!item) return null;
        interval = intersect(interval!, item);
      }
    if (!interval) return null;
    alternatives.push(interval);
  }
  return alternatives;
}

/** The value in quotes after a space, when it is short plain text. */
function quoted(value: string) {
  return QUOTABLE.test(value) ? ` "${value}"` : '';
}

/** A release line: Node.js by major version, Bun by minor version. */
function line(runtime: Runtime, key: Key) {
  if (runtime === 'node') return `Node.js ${key[0]}`;
  return `Bun ${key[0]}.${key[1] === UNBOUNDED ? 'x' : key[1]}`;
}

function oldestSupported(runtime: Runtime) {
  const [major, minor] = OLDEST_LINES[runtime];
  return runtime === 'node' ? `${major}` : `${major}.${minor}`;
}

export type RuntimeRequest = {
  runtime: Runtime;
  /** Where the request is: engines.node, packageManager, .nvmrc, ... */
  field: string;
  /** As written there, such as ">=22 <23", "bun@1.3.4" or "lts/*". */
  value: string;
  /** The version part of value: "1.3.4" for "bun@1.3.4". */
  wanted: string;
  /** null asks for the newest long-term support release (lts/*). */
  versions: VersionRange | null;
};

export function requestSource(request: RuntimeRequest) {
  return `${request.field} ${request.value}`;
}

/** "Node.js >=22 <23 from engines.node". */
export function describeRequest(request: RuntimeRequest) {
  return `${runtimeNames[request.runtime]} ${request.wanted} from ${request.field}`;
}

function checked(request: RuntimeRequest): RuntimeRequest {
  const alternatives = request.versions!;
  const lows = alternatives.map(lowest);
  const found = lows.filter((key): key is Key => key !== null);
  const name = runtimeNames[request.runtime];
  if (!found.length)
    throw new RuntimeVersionError(
      `${request.field}${quoted(request.value)} doesn't match any ${name} version.`,
    );
  const [major, minor] = OLDEST_LINES[request.runtime];
  const floor: Interval = { low: { key: [major, minor, 0, 0], inclusive: true }, high: null };
  if (alternatives.some((item) => lowest(intersect(item, floor)) !== null)) return request;
  const newestKey = alternatives
    .filter((_item, index) => lows[index] !== null)
    .map(highest)
    .reduce((best, key) => (compare(key, best) > 0 ? key : best));
  const oldestKey = found.reduce((best, key) => (compare(key, best) < 0 ? key : best));
  const newest = line(request.runtime, newestKey);
  const asks = line(request.runtime, oldestKey) === newest ? newest : `${newest} or earlier`;
  throw new RuntimeVersionError(
    `${request.field}${quoted(request.value)} asks for ${asks}, older than the oldest ` +
      `supported version (${oldestSupported(request.runtime)}).`,
  );
}

function rangeRequest(runtime: Runtime, field: string, value: string): RuntimeRequest {
  // Count code points, as the build does.
  if ([...value].length > MAXIMUM_REQUEST_LENGTH)
    throw new RuntimeVersionError(`${field} is longer than ${MAXIMUM_REQUEST_LENGTH} characters.`);
  const versions = parseRange(value);
  if (versions) return checked({ runtime, field, value, wanted: value, versions });
  if (runtime === 'node' && VERSION_FILES.node.includes(field) && ALIAS.test(value))
    throw new RuntimeVersionError(
      `${field} asks for${quoted(value)}. Use a version number or range, or lts/*.`,
    );
  throw new RuntimeVersionError(`${field}${quoted(value)} isn't a valid version range.`);
}

/** A version file's request: its first line that isn't blank or a comment. */
export function versionFileRequest(text: string) {
  for (const raw of text.replace(/^﻿/, '').split('\n')) {
    const value = raw.split('#')[0].replace(/^[ \t\r\v\f]+|[ \t\r\v\f]+$/g, '');
    if (value) return value;
  }
  return '';
}

/**
 * The version a commit asks for, or null to use the platform default.
 * readFile returns a root version file's text, or null when there is none;
 * it throws RuntimeVersionError when the file can't be used.
 */
export async function runtimeRequest(
  runtime: Runtime,
  pkg: Record<string, unknown>,
  readFile: (name: string) => Promise<string | null>,
): Promise<RuntimeRequest | null> {
  const manager = pkg.packageManager;
  // packageManager naming npm, pnpm or yarn is about installs, not the runtime.
  if (runtime === 'bun' && typeof manager === 'string' && manager.startsWith('bun@')) {
    const wanted = manager.slice('bun@'.length).split('+')[0];
    const version = parseVersion(wanted);
    if (!version)
      throw new RuntimeVersionError(
        `packageManager${quoted(manager)} must name an exact Bun version, like bun@1.3.4.`,
      );
    const key: Key = [...version, 1];
    return checked({
      runtime,
      field: 'packageManager',
      value: `bun@${wanted}`,
      wanted,
      versions: [{ low: { key, inclusive: true }, high: { key, inclusive: true } }],
    });
  }
  const engines = pkg.engines;
  const field = `engines.${runtime}`;
  if (
    engines &&
    typeof engines === 'object' &&
    !Array.isArray(engines) &&
    Object.hasOwn(engines, runtime)
  ) {
    const value = (engines as Record<string, unknown>)[runtime];
    if (typeof value !== 'string')
      throw new RuntimeVersionError(
        `${field} must be a version range in quotes, like "${EXAMPLES[runtime]}".`,
      );
    return rangeRequest(runtime, field, value);
  }
  for (const name of VERSION_FILES[runtime]) {
    const text = await readFile(name);
    const value = text === null ? '' : versionFileRequest(text);
    if (!value) continue;
    if (runtime === 'node' && value === 'lts/*')
      return { runtime, field: name, value, wanted: value, versions: null };
    return rangeRequest(runtime, name, value);
  }
  return null;
}
