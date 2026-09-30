/**
 * Turning what the API returns into what the review screen renders.
 *
 * Two endpoints feed one card, on purpose. `GET .../findings` carries the verdict and its
 * provenance — enough for the list — and `.../findings/{id}/chain` carries the operands, the
 * arithmetic and the evidence. Fetching the chain for every row on load would be one call per
 * finding for a screen where most are never opened, and the control plane does short work only.
 */

import { getFindingChain, listFindings } from './client';
import type { Evidence, Finding, Outcome, ReviewerAction, Severity, Trace } from '../data/types';

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
    // Read back from the server rather than reset to null on every load. This was `null`
    // unconditionally, so a reviewer who refreshed saw their own decisions vanish from the screen
    // while the ledger still held them — and could record the same one twice, because the first was
    // invisible. The two disagreeing was worse than either being empty.
    reviewer_action: (listed.reviewer_action?.action ?? null) as ReviewerAction | null,
    reviewed_by: listed.reviewer_action?.actor ?? null,
  };
}

/** The arithmetic behind one verdict, folded into the card the reviewer already has open. */
export function withChain(finding: Finding, chain: Chain): Finding {
  const operands = chain.operands ?? [];
  const tracedSources = new Map(
    chain.trace.kind === 'calculation'
      ? chain.trace.operands.map((operand) => [operand.name, operand.source])
      : [],
  );
  const evidence = operands
    .map((operand) => operand.evidence)
    .filter((item): item is NonNullable<typeof item> => item !== null);
  const recordedOperands = operands.map((operand) => ({
    name: operand.name,
    // These are the immutable exact fields from the finding chain.  Do not turn them into a
    // JavaScript number: the evidence view is explanatory and must not silently round a value.
    value: operand.denominator === '1'
      ? `${operand.numerator} ${operand.unit}`
      : `${operand.numerator}/${operand.denominator} ${operand.unit}`,
    source: tracedSources.get(operand.name) ?? operand.evidence?.document_role ?? 'RECORDED',
    status: operand.evidence_status,
    hasEvidence: operand.evidence !== null,
    documentRole: operand.evidence?.document_role,
    canonicalObservationId: operand.evidence?.canonical_observation_id,
  }));

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
          // These strings come from the persisted deterministic calculation trace.  They are
          // displayed verbatim: the UI never reconstructs an exact value from a display value or
          // from floating point.  The chain operands contribute only review/evidence status.
          operands: source.operands.map((operand) => ({
            name: operand.name,
            value: operand.value,
            status: operands.find((item) => item.name === operand.name)?.evidence_status ?? 'RECORDED',
            source: operand.source,
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

  return {
    ...finding,
    recorded_operands: recordedOperands,
    trace,
    arch_evidence: _evidenceFor(evidence, 'ARCH'),
    shop_evidence: _evidenceFor(evidence, 'SHOP'),
  };
}

/** Evidence is carried by the finding chain, never inferred from a card or its rule id. */
function _evidenceFor(
  evidence: readonly NonNullable<Chain['operands'][number]['evidence']>[],
  role: 'ARCH' | 'SHOP',
): Evidence | null {
  const located = evidence.find((item) => item.document_role === role)
    ?? evidence.find((item) => item.document_role.toUpperCase() === role);
  if (!located) return null;

  const polygon = located.polygon.map(([x, y]) => [Number(x), Number(y)] as [number, number]);
  if (polygon.some(([x, y]) => !Number.isFinite(x) || !Number.isFinite(y))) return null;

  return {
    canonical_observation_id: located.canonical_observation_id,
    // The API persists page indexes from zero; people holding a PDF count pages from one.
    page: located.page_index + 1,
    polygon,
    semantic_type: located.semantic_type,
  };
}

/** Every finding for a package, in the order the API ranks them. */
export async function loadFindings(projectId: string, packageId: string): Promise<Finding[]> {
  const page = await listFindings(projectId, packageId);
  const base = page.items.map(toFinding);

  // **Every finding arrives with its evidence, rather than one at a time on request.**
  //
  // The list endpoint carries a finding's identity and outcome and nothing about where its numbers
  // came from — that lives on the chain. So until somebody clicked "Evidence & facts" on a
  // particular card, every card on the page showed no sheet, no page and no operand: a review
  // screen that could not answer "where did this number come from?" without being asked nine
  // separate times. A reviewer's first question about a failure is exactly that question.
  //
  // One request per finding, in parallel. These are small reads of already-computed rows, and a
  // review has single figures of findings — the cost is a fraction of a second against a page that
  // otherwise cannot show its own evidence.
  //
  // **A chain that will not load costs its own card's detail and nothing else.** The finding is
  // still shown, with its outcome and rule, because an evidence lookup failing is not a reason to
  // hide a recorded failure from the person reviewing it.
  const chains = await Promise.all(
    base.map(async (finding) => {
      try {
        return await getFindingChain(projectId, packageId, finding.id);
      } catch {
        return null;
      }
    }),
  );

  return base.map((finding, index) => {
    const chain = chains[index];
    return chain === null ? finding : withChain(finding, chain);
  });
}
