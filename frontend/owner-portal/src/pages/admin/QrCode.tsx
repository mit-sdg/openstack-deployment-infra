import qrcode from 'qrcode-generator';
import { useMemo } from 'react';

// qrcode-generator 2.0.4 (MIT, Kazuhiko Arase) computes the module matrix;
// rendering is plain SVG elements, so nothing is injected as markup and no
// style attributes are needed. See frontend/THIRD_PARTY_NOTICES.md.

/** Quiet zone in modules, as the QR specification requires. */
export const QUIET_ZONE = 4;

/**
 * The QR module matrix for text, encoded locally as UTF-8 bytes with
 * error-correction level M. `true` is a dark module. Nothing leaves the page.
 */
export function qrMatrix(text: string): boolean[][] {
  // The library takes one byte per character, so pass UTF-8 bytes that way.
  const bytes = String.fromCharCode(...new TextEncoder().encode(text));
  const code = qrcode(0, 'M');
  code.addData(bytes, 'Byte');
  code.make();
  const size = code.getModuleCount();
  return Array.from({ length: size }, (_, row) =>
    Array.from({ length: size }, (_, column) => code.isDark(row, column)),
  );
}

/**
 * A scannable QR code: dark modules on a white plate with a quiet zone, in
 * both themes. It scales to its container's width.
 */
export function QrCode({ value, label }: { value: string; label: string }) {
  const matrix = useMemo(() => qrMatrix(value), [value]);
  const size = matrix.length + 2 * QUIET_ZONE;
  const path = matrix
    .flatMap((cells, row) =>
      cells.map((dark, column) =>
        dark ? `M${column + QUIET_ZONE} ${row + QUIET_ZONE}h1v1h-1z` : '',
      ),
    )
    .join('');
  return (
    <svg
      className="admin-qr"
      viewBox={`0 0 ${size} ${size}`}
      role="img"
      aria-label={label}
      shapeRendering="crispEdges"
    >
      <rect className="admin-qr__plate" width={size} height={size} />
      <path className="admin-qr__modules" d={path} />
    </svg>
  );
}
