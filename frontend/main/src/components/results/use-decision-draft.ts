import { useState } from 'react';

import type { Finding } from '@/data/types';
import type { DecisionSaveResult, SimpleReviewAction } from '@/components/chat/decisionSave';
import { actionNeedsNote } from '@/components/output/reviewerResults';

export type Choice = 'confirm' | 'problem' | 'dismiss';
export type Problem = 'correct' | 'except';

export interface DecideHandlers {
  onAction: (id: string, action: SimpleReviewAction, note?: string) => Promise<DecisionSaveResult>;
  onCorrect: (id: string, value: string) => Promise<DecisionSaveResult>;
  onExcept: (id: string, reason: string, expiresAt: string) => Promise<DecisionSaveResult>;
}

/**
 * One reviewer decision being written (#1039, shared with the queue in #1050): the finding card's
 * three choices with the same calls and the same rules — the note is required exactly where
 * `actionNeedsNote` says, a correction needs its value with a unit, an exception needs a reason and
 * a future end date. The Decide dialog and the "Needs you" queue both use this, so they cannot drift.
 */
export function useDecisionDraft(finding: Pick<Finding, 'id' | 'outcome'> | null) {
  const [choice, setChoice] = useState<Choice | ''>('');
  const [problem, setProblem] = useState<Problem | ''>('');
  const [note, setNote] = useState('');
  const [value, setValue] = useState('');
  const [expires, setExpires] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const outcome = finding?.outcome ?? 'REVIEW_REQUIRED';
  const abstention = outcome === 'REVIEW_REQUIRED' || outcome === 'NOT_FOUND';
  const labels = {
    confirm: abstention ? 'Checked: OK' : 'Confirm finding',
    problem: 'Problem',
    dismiss: abstention ? 'Not checkable' : 'Dismiss',
  };
  const simple: SimpleReviewAction | null = choice === 'confirm' || choice === 'dismiss' ? choice : null;
  const noteRequired = simple !== null && actionNeedsNote(outcome, simple);
  const today = new Date().toISOString().slice(0, 10);
  const ready =
    finding !== null &&
    ((simple !== null && (!noteRequired || note.trim().length > 0)) ||
      (choice === 'problem' && problem === 'correct' && value.trim().length > 0) ||
      (choice === 'problem' && problem === 'except' && note.trim().length > 0 && expires > today));

  function reset() {
    setChoice('');
    setProblem('');
    setNote('');
    setValue('');
    setExpires('');
    setError(null);
  }

  function choose(next: Choice | '') {
    setChoice(next);
    setError(null);
  }

  /** Record the decision through the page's own handlers. True when the server confirmed it. */
  async function save(handlers: DecideHandlers): Promise<boolean> {
    if (!finding || !ready || saving) return false;
    setSaving(true);
    setError(null);
    try {
      let result: DecisionSaveResult;
      if (simple !== null) {
        result = await handlers.onAction(finding.id, simple, note.trim() || undefined);
      } else if (problem === 'correct') {
        result = await handlers.onCorrect(finding.id, value.trim());
      } else {
        // Midday rather than midnight, as on the finding card: a date input carries no time.
        result = await handlers.onExcept(finding.id, note.trim(), new Date(`${expires}T12:00:00Z`).toISOString());
      }
      if (result.saved) {
        reset();
        return true;
      }
      setError(result.error);
      return false;
    } catch (caught) {
      setError(`Save was not confirmed — ${caught instanceof Error ? caught.message : String(caught)}`);
      return false;
    } finally {
      setSaving(false);
    }
  }

  return {
    outcome, choice, problem, note, value, expires, saving, error, labels, simple, noteRequired, ready, today,
    choose, setProblem, setNote, setValue, setExpires, reset, save,
  };
}

export type DecisionDraft = ReturnType<typeof useDecisionDraft>;
