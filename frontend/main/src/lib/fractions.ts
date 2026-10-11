/**
 * One way to print an exact fraction on screen (#1183): as a mixed number, `2 1/2`, never `5/2`.
 *
 * The API sends exact values as a numerator and a denominator (decimal strings), and some text it
 * sends is a fraction written the way Python writes one (`5/2 in`, the rulebook's default). A
 * reviewer reads `5/2 in` as a typo. Everything here is exact: whole-number text and `BigInt`, never
 * a float, so the printed number is the stored number.
 */

const INTEGER = /^\s*([+-]?)(\d+)\s*$/;

function gcd(a: bigint, b: bigint): bigint {
  let x = a;
  let y = b;
  while (y !== 0n) [x, y] = [y, x % y];
  return x;
}

/**
 * `numerator / denominator` as a mixed number in lowest terms: `5, 2` → `2 1/2`, `31, 16` →
 * `1 15/16`, `4, 1` → `4`, `-5, 2` → `-2 1/2`, `0, 7` → `0`.
 *
 * `null` for anything that is not two whole numbers or that divides by zero, so a caller shows its
 * own "not available" rather than a number this made up.
 */
export function mixedNumber(numerator: string | number | bigint, denominator: string | number | bigint): string | null {
  const n = INTEGER.exec(String(numerator));
  const d = INTEGER.exec(String(denominator));
  if (!n || !d) return null;
  const top = BigInt(n[2]);
  const bottom = BigInt(d[2]);
  if (bottom === 0n) return null;
  const negative = top !== 0n && (n[1] === '-') !== (d[1] === '-');
  const divisor = gcd(top, bottom);
  const reducedTop = top / divisor;
  const reducedBottom = bottom / divisor;
  const whole = reducedTop / reducedBottom;
  const rest = reducedTop % reducedBottom;
  const sign = negative ? '-' : '';
  if (rest === 0n) return `${sign}${whole}`;
  if (whole === 0n) return `${sign}${rest}/${reducedBottom}`;
  return `${sign}${whole} ${rest}/${reducedBottom}`;
}

/** An exact value with its unit, `2 1/2 in`; the bare fraction when the parts are not whole numbers. */
export function mixedQuantity(value: { numerator: string; denominator: string; unit?: string | null }): string {
  const number = mixedNumber(value.numerator, value.denominator) ?? `${value.numerator}/${value.denominator}`;
  return value.unit ? `${number} ${value.unit}` : number;
}

/**
 * Text with every improper fraction rewritten as a mixed number: `5/2 in` → `2 1/2 in`. Proper
 * fractions, mixed numbers (`25 1/2"`), decimals and words are left exactly as they are.
 */
export function mixedFractionsIn(text: string): string {
  return text.replace(/(?<![\d./])(-?)(\d+)\/(\d+)(?![\d./])/g, (match, minus: string, top: string, bottom: string, offset: number, source: string) => {
    // "2 1/2" is a mixed number already: a whole number straight before the fraction.
    if (/\d\s+$/.test(source.slice(0, offset))) return match;
    if (BigInt(top) < BigInt(bottom)) return match;
    return mixedNumber(`${minus}${top}`, bottom) ?? match;
  });
}
