import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { QUIET_ZONE, QrCode, qrMatrix } from './QrCode';

// An independent decoder for clean (error-free) matrices, written from the
// QR specification (ISO/IEC 18004): it reads the format information, removes
// the mask, reads codewords in zigzag order, de-interleaves the blocks,
// checks every block's Reed–Solomon codewords and parses byte mode. It only
// knows level M for versions 1–10, which covers otpauth URIs.

// Level M blocks per version: [error-correction codewords per block, [count, data codewords]...].
const levelM: Record<number, [number, ...[number, number][]]> = {
  1: [10, [1, 16]],
  2: [16, [1, 28]],
  3: [26, [1, 44]],
  4: [18, [2, 32]],
  5: [24, [2, 43]],
  6: [16, [4, 27]],
  7: [18, [4, 31]],
  8: [22, [2, 38], [2, 39]],
  9: [22, [3, 36], [2, 37]],
  10: [26, [4, 43], [1, 44]],
};
const alignment: Record<number, number[]> = {
  1: [],
  2: [6, 18],
  3: [6, 22],
  4: [6, 26],
  5: [6, 30],
  6: [6, 34],
  7: [6, 22, 38],
  8: [6, 24, 42],
  9: [6, 26, 46],
  10: [6, 28, 50],
};
const masks: ((r: number, c: number) => boolean)[] = [
  (r, c) => (r + c) % 2 === 0,
  (r) => r % 2 === 0,
  (_, c) => c % 3 === 0,
  (r, c) => (r + c) % 3 === 0,
  (r, c) => (Math.floor(r / 2) + Math.floor(c / 3)) % 2 === 0,
  (r, c) => ((r * c) % 2) + ((r * c) % 3) === 0,
  (r, c) => (((r * c) % 2) + ((r * c) % 3)) % 2 === 0,
  (r, c) => (((r + c) % 2) + ((r * c) % 3)) % 2 === 0,
];

// GF(256) with the QR polynomial x^8 + x^4 + x^3 + x^2 + 1.
const exp: number[] = [];
const log: number[] = [];
for (let i = 0, x = 1; i < 255; i++, x = x & 0x80 ? ((x << 1) ^ 0x11d) & 0xff : x << 1) {
  exp[i] = x;
  log[x] = i;
}
const multiply = (a: number, b: number) => (a && b ? exp[(log[a] + log[b]) % 255] : 0);
function reedSolomon(data: number[], degree: number) {
  let generator = [1];
  for (let i = 0; i < degree; i++) {
    const next = new Array(generator.length + 1).fill(0);
    generator.forEach((g, j) => {
      next[j] ^= g;
      next[j + 1] ^= multiply(g, exp[i]);
    });
    generator = next;
  }
  const remainder = new Array(degree).fill(0);
  for (const byte of data) {
    const factor = byte ^ remainder.shift();
    remainder.push(0);
    generator.slice(1).forEach((g, j) => (remainder[j] ^= multiply(g, factor)));
  }
  return remainder;
}

function bch(value: number) {
  let r = value << 10;
  for (let bit = 14; bit >= 10; bit--) if ((r >> bit) & 1) r ^= 0x537 << (bit - 10);
  return (value << 10) | r;
}

function decode(m: boolean[][]) {
  const size = m.length;
  const version = (size - 17) / 4;
  expect(Number.isInteger(version) && levelM[version]).toBeTruthy();
  // Format information, top-left copy (bit 14 is the most significant).
  const at = (r: number, c: number) => (m[r][c] ? 1 : 0);
  const positions: [number, number][] = [
    ...[0, 1, 2, 3, 4, 5].map((i) => [i, 8] as [number, number]),
    [7, 8],
    [8, 8],
    [8, 7],
    ...[9, 10, 11, 12, 13, 14].map((i) => [8, 14 - i] as [number, number]),
  ];
  const format = positions.reduce((bits, [r, c], i) => bits | (at(r, c) << i), 0) ^ 0x5412;
  const header = format >> 10;
  expect(bch(header)).toBe(format); // BCH-valid format information
  expect(header >> 3).toBe(0); // error-correction level M
  const mask = masks[header & 7];
  // Function patterns, which carry no data.
  const reserved = Array.from({ length: size }, () => new Array<boolean>(size).fill(false));
  const fill = (r0: number, c0: number, rows: number, cols: number) => {
    for (let r = r0; r < r0 + rows; r++) for (let c = c0; c < c0 + cols; c++) reserved[r][c] = true;
  };
  fill(0, 0, 9, 9);
  fill(0, size - 8, 9, 8);
  fill(size - 8, 0, 8, 9);
  fill(6, 0, 1, size);
  fill(0, 6, size, 1);
  const centers = alignment[version];
  for (const r of centers)
    for (const c of centers) {
      const corner =
        (r === 6 && c === 6) || (r === 6 && c === size - 7) || (r === size - 7 && c === 6);
      if (!corner) fill(r - 2, c - 2, 5, 5);
    }
  if (version >= 7) {
    fill(0, size - 11, 6, 3);
    fill(size - 11, 0, 3, 6);
  }
  // Codewords in zigzag order, two columns at a time from the bottom right.
  const [ecPerBlock, ...groups] = levelM[version];
  const blocks = groups.flatMap(([count, length]) => new Array(count).fill(length) as number[]);
  const total = blocks.reduce((sum, length) => sum + length + ecPerBlock, 0);
  const codewords: number[] = new Array(total).fill(0);
  let bit = 0;
  for (let right = size - 1; right >= 1; right -= 2) {
    if (right === 6) right = 5;
    for (let vertical = 0; vertical < size; vertical++)
      for (let j = 0; j < 2; j++) {
        const c = right - j;
        const upward = ((right + 1) & 2) === 0;
        const r = upward ? size - 1 - vertical : vertical;
        if (reserved[r][c] || bit >= total * 8) continue;
        const value = (m[r][c] !== mask(r, c) ? 1 : 0) << (7 - (bit % 8));
        codewords[bit >> 3] |= value;
        bit++;
      }
  }
  expect(bit).toBe(total * 8);
  // De-interleave and check each block's error-correction codewords.
  const data = blocks.map(() => [] as number[]);
  const ec = blocks.map(() => [] as number[]);
  let index = 0;
  for (let i = 0; i < Math.max(...blocks); i++)
    blocks.forEach((length, b) => i < length && data[b].push(codewords[index++]));
  for (let i = 0; i < ecPerBlock; i++) blocks.forEach((_, b) => ec[b].push(codewords[index++]));
  blocks.forEach((_, b) => expect(reedSolomon(data[b], ecPerBlock)).toEqual(ec[b]));
  // Byte mode: mode 0100, an 8-bit count (versions 1–9) or 16-bit (10+), then bytes.
  const stream = data.flat();
  let cursor = 0;
  const read = (width: number) => {
    let value = 0;
    for (let i = 0; i < width; i++, cursor++)
      value = (value << 1) | ((stream[cursor >> 3] >> (7 - (cursor % 8))) & 1);
    return value;
  };
  expect(read(4)).toBe(0b0100);
  const length = read(version < 10 ? 8 : 16);
  const bytes = Uint8Array.from({ length }, () => read(8));
  expect(read(4)).toBe(0); // terminator
  return new TextDecoder('utf-8', { fatal: true }).decode(bytes);
}

describe('authenticator QR code', () => {
  const uri =
    'otpauth://totp/App%20platform:admin42?secret=JBSWY3DPEHPK3PXPJBSWY3DPEHPK3PXP&issuer=App%20platform&algorithm=SHA1&digits=6&period=30';

  it.each([
    ['a short value', 'HELLO'],
    ['an otpauth URI', uri],
    ['non-ASCII text as UTF-8', 'otpauth://totp/Café:ünïcode?secret=JBSWY3DP&issuer=Café'],
    ['a long URI that needs version 7 or more', uri + '&image=' + 'x'.repeat(60)],
  ])('round-trips %s through an independent decoder', (_, text) => {
    expect(decode(qrMatrix(text))).toBe(text);
  });

  it('exercises version 7+ and rejects a corrupted matrix', () => {
    const long = qrMatrix(uri + '&image=' + 'x'.repeat(60));
    expect((long.length - 17) / 4).toBeGreaterThanOrEqual(7);
    const matrix = qrMatrix(uri);
    const last = matrix.length - 1;
    // The bottom-right module always holds the first data bit.
    matrix[last][last] = !matrix[last][last];
    expect(() => decode(matrix)).toThrow();
  });

  it('renders SVG with a white plate, a 4-module quiet zone and no style attributes', () => {
    const { container } = render(<QrCode value={uri} label="QR code for your authenticator" />);
    const svg = container.querySelector('svg')!;
    const size = qrMatrix(uri).length + 2 * QUIET_ZONE;
    expect(svg.getAttribute('viewBox')).toBe(`0 0 ${size} ${size}`);
    expect(svg).toHaveAttribute('role', 'img');
    expect(svg).toHaveAccessibleName('QR code for your authenticator');
    expect(container.querySelector('[style]')).toBeNull();
    expect(container.querySelector('.admin-qr__plate')).toHaveAttribute('width', String(size));
    // No module is drawn inside the quiet zone.
    const starts = [
      ...svg
        .querySelector('path')!
        .getAttribute('d')!
        .matchAll(/M(\d+) (\d+)/g),
    ];
    expect(starts.length).toBeGreaterThan(0);
    for (const [, x, y] of starts) {
      expect(Number(x)).toBeGreaterThanOrEqual(QUIET_ZONE);
      expect(Number(y)).toBeGreaterThanOrEqual(QUIET_ZONE);
      expect(Number(x)).toBeLessThan(size - QUIET_ZONE);
      expect(Number(y)).toBeLessThan(size - QUIET_ZONE);
    }
  });
});
