/**
 * A typed width is more than zero (#1182). A `0` typed for a piece nobody could read, or a stray
 * minus sign, can make a countertop row add up by accident, so it is refused before it is sent. The
 * server refuses it too (422); this only saves the round trip. Text the check cannot understand is
 * left to the server, which explains it.
 */
export const WIDTH_NOT_ABOVE_ZERO =
  'A width or length must be more than zero. If you cannot read it, leave it empty: a missing piece is never zero.';

export function widthNotAboveZero(typed: string): boolean {
  const text = typed.trim();
  if (text.startsWith('-') || text.startsWith('−')) return true;
  // Every digit outside a denominator: `0`, `0 in`, `0"`, `0.0` and `0/16` have only zeros.
  const counted = text.replace(/\/\s*\d+/g, '').match(/\d/g);
  return counted !== null && counted.every((digit) => digit === '0');
}
