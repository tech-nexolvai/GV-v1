/**
 * Rendering exact rationals, without ever letting them become floating point.
 *
 * The API sends `numerator` and `denominator` as **decimal strings**, and
 * `app/api/finding_chain.py` says why: "JSON numbers are exact on the wire, but common clients turn
 * them into binary floating-point values and can silently change a large numerator." Those columns
 * are `BIGINT` in PostgreSQL, and JavaScript's `Number` loses integers above 2^53 — so parsing one
 * with `Number()` or `parseInt` can change the value with no error anywhere.
 *
 * Everything here therefore uses `BigInt`. Nothing in this file produces a `number`.
 *
 * This matters more under V1 than it would have under a tolerance band. Raj settled on exact match
 * (`docs/decisions/V1_VERDICT_MODEL.md` D1), so there is no band to absorb a rounding error — a
 * value that shifts by one part in 2^53 is simply a different verdict.
 */

export interface ExactValue {
  numerator: string;
  denominator: string;
}

/** Greatest common divisor, for reducing before display. */
function gcd(a: bigint, b: bigint): bigint {
  let x = a < 0n ? -a : a;
  let y = b < 0n ? -b : b;
  while (y) {
    [x, y] = [y, x % y];
  }
  return x;
}

/**
 * Render an exact rational the way a reviewer reads a drawing: `38 3/4`, never `38.75`.
 *
 * A decimal would misrepresent what the engine compared — the check is exact equality on fractions,
 * so showing a rounded decimal beside a flag invites the reviewer to conclude the numbers match when
 * the engine found they do not.
 *
 * Whole numbers render bare (`96`), proper fractions without a whole part (`3/4`), and anything
 * negative keeps its sign on the front (`-2 1/2`).
 */
export function formatExact(value: ExactValue): string {
  const numerator = BigInt(value.numerator);
  const denominator = BigInt(value.denominator);
  if (denominator === 0n) {
    throw new RangeError('a rational with a zero denominator is not a number');
  }

  const negative = numerator < 0n !== denominator < 0n;
  let n = numerator < 0n ? -numerator : numerator;
  let d = denominator < 0n ? -denominator : denominator;

  const divisor = gcd(n, d);
  if (divisor > 1n) {
    n /= divisor;
    d /= divisor;
  }

  const sign = negative ? '-' : '';
  if (d === 1n) return `${sign}${n}`;

  const whole = n / d;
  const remainder = n % d;
  if (whole === 0n) return `${sign}${remainder}/${d}`;
  return `${sign}${whole} ${remainder}/${d}`;
}

/** Compare two exact rationals without dividing. Cross-multiplication stays in `BigInt`. */
export function compareExact(left: ExactValue, right: ExactValue): -1 | 0 | 1 {
  const a = BigInt(left.numerator) * BigInt(right.denominator);
  const b = BigInt(right.numerator) * BigInt(left.denominator);
  return a < b ? -1 : a > b ? 1 : 0;
}
