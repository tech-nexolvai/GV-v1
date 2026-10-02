type Receipt = { semantic_type: string };
export type ConfirmationLedger = {
  confirmed: Map<string, Receipt>;
  pending: Map<string, { semanticType: string; request: Promise<Receipt> }>;
};

export function newConfirmationLedger(): ConfirmationLedger {
  return { confirmed: new Map(), pending: new Map() };
}

/** The server refuses duplicate confirmation. Reuse only a receipt we actually received. */
export async function confirmCandidateOnce(
  ledger: ConfirmationLedger,
  candidateId: string,
  semanticType: string,
  confirm: (candidateId: string, semanticType: string) => Promise<Receipt>,
): Promise<Receipt> {
  const saved = ledger.confirmed.get(candidateId);
  const pending = ledger.pending.get(candidateId);
  if ((saved && saved.semantic_type !== semanticType) || (pending && pending.semanticType !== semanticType)) {
    throw new Error('This reading was already assigned a different meaning. Inspect its recorded confirmation before continuing.');
  }
  if (saved) return saved;
  if (pending) return pending.request;
  const request = Promise.resolve().then(() => confirm(candidateId, semanticType)).then((receipt) => {
    if (receipt.semantic_type !== semanticType) {
      throw new Error('The confirmation response did not match the selected meaning. Refresh and inspect the recorded reading.');
    }
    ledger.confirmed.set(candidateId, receipt);
    return receipt;
  });
  ledger.pending.set(candidateId, { semanticType, request });
  try {
    return await request;
  } finally {
    ledger.pending.delete(candidateId);
  }
}

export async function confirmProposalFields(
  fields: readonly { key: string; semanticType: string; candidateIds: readonly string[] }[],
  ledger: ConfirmationLedger,
  confirm: (candidateId: string, semanticType: string) => Promise<Receipt>,
): Promise<Set<string>> {
  const confirmed = new Set<string>();
  for (const field of fields) {
    for (const candidateId of field.candidateIds) {
      try {
        await confirmCandidateOnce(ledger, candidateId, field.semanticType, confirm);
      } catch (error) {
        const reason = error instanceof Error ? error.message : String(error);
        throw new Error(`Drawing confirmation stopped for ${field.key}: ${reason}. Your entries and successful confirmations are retained. Save and checks were stopped; no drawing reading was replaced with a typed value.`, { cause: error });
      }
    }
    if (field.candidateIds.length > 0) confirmed.add(field.key);
  }
  return confirmed;
}

/** Equal widths can represent different cabinets. Candidate identity, never value, deduplicates. */
export function appendConfirmedRunValue(
  current: readonly string[], value: string, candidateId: string, representedIds: readonly string[] = [],
): string[] {
  if (representedIds.includes(candidateId)) return [...current];
  return [...current.filter((entry) => entry.trim()), value];
}
