/**
 * Turning what the API returns into what the review screen renders.
 *
 * Two endpoints feed one card, on purpose. `GET .../findings` carries the verdict and its
 * provenance — enough for the list — and `.../findings/{id}/chain` carries the operands, the
 * arithmetic and the evidence. Fetching the chain for every row on load would be one call per
 * finding for a screen where most are never opened, and the control plane does short work only.
 */

import { getFindingChain, listFindings } from './client';
import { formatExact } from './fractions';
import type { Finding, Outcome, Severity, Trace } from '../data/mock';

type Listed = Awaited<ReturnType<typeof listFindings>>['items'][number];
type Chain = Awaited<ReturnType<typeof getFindingChain>>;

/**
 * A row from the list, with the chain-derived fields left absent.
 *
 * Absent rather than blank: the card renders each of them conditionally, so a row shows the verdict
 * it has and does not display an empty "expected" that reads as a value of nothing.
 */
export function toFinding(listed: Listed): Finding {
  return {
    id: listed.id,
    check_id: listed.rule_id,
    // The rule id until the snapshot's human name is on the wire. Better a real identifier than a
    // placeholder sentence nobody can look up.
    name: listed.rule_id,
    outcome: listed.outcome as Outcome,
    severity: listed.severity as Severity,
    reviewer_action: null,
  };
}

/** The arithmetic behind one verdict, folded into the card the reviewer already has open. */
export function withChain(finding: Finding, chain: Chain): Finding {
  const operands = chain.operands ?? [];

  // `trace` is a discriminated union now, so this narrows instead of guessing. It used to be a
  // free-form dict and this function read fields out of it with a string guard — the one place the
  // generated types could not check anything.
  //
  // The abstention case matters as much as the calculation one: `app/budget/overflow.py` writes a
  // trace with a cause and no arithmetic, and rendering that as a calculation with no operands would
  // say "the check ran and found nothing" about a check that never ran.
  const source = chain.trace;
  const trace: Trace =
    source.kind === 'calculation'
      ? {
          operation: source.operation,
          // Values are exact rationals and arrive as text. `formatExact` keeps them that way, using
          // BigInt — under exact match a value shifted by binary rounding is a different verdict.
          operands: operands.map((operand) => ({
            name: operand.name,
            value: formatExact({
              numerator: operand.numerator,
              denominator: operand.denominator,
            }),
            status: operand.evidence_status,
            source: operand.unit,
          })),
          comparison: source.comparison ?? '',
        }
      : {
          operation: source.kind === 'abstention' ? source.cause : 'unrecognised trace',
          operands: [],
          comparison:
            source.kind === 'abstention'
              ? (source.reason ?? 'The check did not run.')
              : 'This trace was not recognised and is shown as stored.',
        };

  return { ...finding, trace };
}

/** Every finding for a package, in the order the API ranks them. */
export async function loadFindings(projectId: string, packageId: string): Promise<Finding[]> {
  const page = await listFindings(projectId, packageId);
  return page.items.map(toFinding);
}
