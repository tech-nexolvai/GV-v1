/**
 * #1531: code's own reading of each side's value at its box (verify_value), shown in plain words under
 * the side's crop, and the drawn-ink witness named in the evidence words.
 */
import { describe, expect, it } from 'vitest';

import type { VerifiedValue } from '@/lib/chat-api';
import { evidenceWords, printedSideWords } from '@/lib/chat-trust';
import { finding } from './fixtures/chat';

function verified(change: Partial<VerifiedValue>): VerifiedValue {
  return { result: 'match', route: 'text', read: '46"', reads: [], reason: '', value: '46"', pending: false, words: '', ...change };
}

describe('verify_value words', () => {
  it('shows what code read at each spot, by route', () => {
    const one = finding('v1', {
      architect: '46"',
      vendor: '1168 [46]',
      checks: {
        printed_architect: 'match',
        printed_vendor: 'no_text',
        verify_architect: verified({ words: 'Read from the drawing’s text: 46"' }),
        verify_vendor: verified({ route: 'drawn', words: 'Read from the vendor’s ink by two OCR engines: 46"' }),
      },
    });
    expect(printedSideWords(one, 'architect')).toEqual({ state: 'pass', words: 'Read from the drawing’s text: 46"' });
    expect(printedSideWords(one, 'vendor')).toEqual({ state: 'pass', words: 'Read from the vendor’s ink by two OCR engines: 46"' });
  });

  it('a mismatch fails and a side still waiting for the drawn ink keeps the older words', () => {
    const one = finding('v2', {
      architect: '46"',
      vendor: '47"',
      checks: {
        printed_architect: 'not_found',
        printed_vendor: 'no_text',
        verify_architect: verified({ result: 'mismatch', read: '45"', words: 'The drawing prints 45" here, not 46"' }),
        verify_vendor: verified({ result: 'cannot_read', route: 'none', read: null, pending: true, words: 'Code could not read this spot: the drawn ink was not read yet' }),
      },
    });
    expect(printedSideWords(one, 'architect')).toEqual({ state: 'fail', words: 'The drawing prints 45" here, not 46"' });
    expect(printedSideWords(one, 'vendor')?.state).toBe('unsure');
    expect(printedSideWords(one, 'vendor')?.words).toContain('pen strokes');
  });

  it('names the drawn-ink witness', () => {
    expect(evidenceWords(finding('v3', { evidence_kind: 'observed', witnesses: ['ocr_ink'] }))).toBe(
      'Printed on the drawing: both values read from the vendor’s drawn ink by two OCR engines',
    );
  });
});
