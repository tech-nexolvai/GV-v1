import { useState } from 'react';
import { ChevronRight, FileSearch, ExternalLink, CheckCircle, TriangleAlert } from 'lucide-react';
import type { Finding } from '../../data/types';
import { OutcomeBadge, SeverityDot } from '../ui/StatusBadge';
import { OutcomeIcon } from '../ui/OutcomeIcon.js';
import { createDecisionSaver, type DecisionSaveResult, type SimpleReviewAction } from './decisionSave.js';
import { actionNeedsNote } from '../output/reviewerResults.js';
import './FindingCard.css';

function readableLabel(value: string): string {
  return value.replaceAll('_', ' ');
}

function readableSource(value: string): string {
  if (value === 'ARCH') return 'Approved / architectural source';
  if (value === 'SHOP') return 'Vendor / shop source';
  if (value === 'USER_INPUT') return 'Reviewer-entered source';
  return value;
}

function readableStatus(value: string): string {
  return value.replaceAll('_', ' ').toLowerCase();
}

interface FindingCardProps {
  finding: Finding;
  isSelected: boolean;
  /** The table row has its own disclosure; the first opening must reveal the detail too. */
  defaultExpanded?: boolean;
  /** Server readiness, not the existence of an action, decides whether review is finished. */
  needsDecision?: boolean;
  onViewEvidence: (finding: Finding) => void;
  onAction: (findingId: string, action: SimpleReviewAction, note?: string) => Promise<DecisionSaveResult>;
  /**
   * Correct a reading, with what it should say.
   *
   * Separate from `onAction` because a correction is not a kind — it is a kind *and a value*, and
   * the two go to a different endpoint. Sending `correct` as a bare kind recorded that something had
   * been corrected without saying to what, and the ledger stayed empty.
   */
  onCorrect: (findingId: string, correctedValue: string) => Promise<DecisionSaveResult>;
  /** Grant an exception, with the reason and the date it runs out. Both required — a permanent
   *  silent exception is how a check gets switched off and nobody remembers. */
  onExcept: (findingId: string, reason: string, expiresAt: string) => Promise<DecisionSaveResult>;
}

export function FindingCard({
  finding,
  isSelected,
  defaultExpanded = false,
  needsDecision,
  onViewEvidence,
  onAction,
  onCorrect,
  onExcept,
}: FindingCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded || finding.outcome === 'FAIL');
  const [showTrace, setShowTrace] = useState(false);
  // Which payload the reviewer is filling in, if either. `null` is the ordinary state: the buttons
  // that need nothing more still act on one click.
  const [pending, setPending] = useState<'correct' | 'except' | SimpleReviewAction | null>(null);
  const [correctedValue, setCorrectedValue] = useState('');
  const [reason, setReason] = useState('');
  const [expiresAt, setExpiresAt] = useState('');
  const [saveDecision] = useState(createDecisionSaver);
  const [isSaving, setIsSaving] = useState(false);
  const [saveError, setSaveError] = useState<string | null>(null);

  const hasAction = finding.reviewer_action !== null;
  const needsNote = finding.outcome === 'REVIEW_REQUIRED' || finding.outcome === 'NOT_FOUND';
  const requiresRerun = finding.reviewer_action === 'correct';
  const unresolved = needsDecision ?? (!hasAction || requiresRerun || (
    needsNote && !finding.reviewer_note?.trim()
  ));

  function submitAction(action: SimpleReviewAction) {
    if (isSaving) return;
    if (actionNeedsNote(finding.outcome, action)) { setPending(action); setSaveError(null); return; }
    void saveDecision(() => onAction(finding.id, action), {
      busy: setIsSaving, error: setSaveError, saved: () => {},
    });
  }

  return (
    <div
      className={`finding-card finding-card--${finding.outcome.toLowerCase().replace('_', '-')} ${isSelected ? 'finding-card--selected' : ''} ${hasAction && !unresolved ? 'finding-card--actioned' : ''}`}
      aria-label={`${finding.check_id}: ${finding.name} — ${finding.outcome}`}
    >
      {/* ── Header row ──────────────────────────────────── */}
      <button
        className="finding-card__header"
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
      >
        <div className="finding-card__header-left">
          <OutcomeIcon outcome={finding.outcome} size={14} className="finding-card__outcome-icon" />
          <span className="finding-card__check-id">{finding.check_id}</span>
          <div className="finding-card__severity">
            <SeverityDot severity={finding.severity} />
          </div>
          <span className="finding-card__name">{finding.name}</span>
        </div>

        <div className="finding-card__header-right">
          {hasAction && (
            <span className="finding-card__action-tag">
              {finding.reviewer_action}
            </span>
          )}
          <OutcomeBadge outcome={finding.outcome} size="sm" />
          {/* One chevron that turns, rather than two that swap. A swap is a cut; the rotation is
              what ties the control to the panel it opens. */}
          <ChevronRight size={13} className="collapsible-chevron" data-open={expanded} />
        </div>
      </button>

      {/* ── Expanded body ──────────────────────────────────
           Kept mounted and collapsed with `grid-template-rows`, so it animates to its natural
           height. Unmounting on close made every open a hard cut and threw away any half-typed
           correction in the form below. */}
      {/* `inert`, not `aria-hidden`. The collapsed body holds eight buttons and three inputs, and
          `aria-hidden` on a subtree containing focusable controls is a WCAG failure: the tab order
          still stops on them while the accessibility tree says they are not there, so a keyboard
          user lands on a confirm button inside a panel their screen reader never announced.
          `inert` removes both at once. */}
      <div className="collapsible" data-open={expanded} inert={!expanded}>
        <div className="finding-card__body">

          {/* Key numbers — only for PASS/FAIL */}
          {(finding.expected || finding.found) && (
            <div className="finding-card__values">
              <div className="finding-card__value-item">
                <span className="finding-card__value-label">Expected</span>
                <span className="finding-card__value-number">{finding.expected ?? '—'}</span>
              </div>
              <div className="finding-card__value-sep" aria-hidden="true">→</div>
              <div className="finding-card__value-item">
                <span className="finding-card__value-label">Found</span>
                <span className={`finding-card__value-number ${finding.outcome === 'FAIL' ? 'finding-card__value-number--fail' : ''}`}>
                  {finding.found ?? '—'}
                </span>
              </div>
              {finding.delta && (
                <>
                  <div className="finding-card__value-sep" aria-hidden="true">=</div>
                  <div className="finding-card__value-item">
                    <span className="finding-card__value-label">Delta</span>
                    <span className={`finding-card__value-number finding-card__value-number--delta ${finding.outcome === 'FAIL' ? 'finding-card__value-number--fail finding-card__value-number--fail-bold' : ''}`}>
                      {finding.outcome === 'FAIL' && <TriangleAlert size={12} className="finding-card__delta-icon" />}
                      Δ {finding.delta}
                    </span>
                  </div>
                  {finding.tolerance && (
                    <div className="finding-card__value-item finding-card__value-item--tol">
                      <span className="finding-card__value-label">Tolerance</span>
                      <span className="finding-card__value-number finding-card__value-number--muted">
                        {finding.tolerance}
                      </span>
                    </div>
                  )}
                </>
              )}
            </div>
          )}

          {/* Reason — for REVIEW/NOT_FOUND */}
          {finding.reason && (
            <p className="finding-card__reason">{finding.reason}</p>
          )}

          {finding.notes && finding.notes.length > 0 && (
            <section className="finding-card__facts" aria-label="Recorded provenance">
              <p className="finding-card__facts-title">Recorded provenance</p>
              <ul className="finding-card__notes">
                {finding.notes.map((note, index) => <li key={index}>{note}</li>)}
              </ul>
            </section>
          )}

          {/* A concise, always-visible rendering of the immutable engine trace. This gives the
              reviewer the actual recorded input and comparison before the optional audit detail. */}
          {finding.trace && finding.trace.operands.length > 0 && (
            <section className="finding-card__facts" aria-label="Recorded check facts">
              <p className="finding-card__facts-title">Recorded check facts</p>
              <div className="finding-card__facts-list">
                {finding.trace.operands.map((op, index) => (
                  <div key={`${op.name}-${index}`} className="finding-card__fact">
                    <span className="finding-card__fact-name">{readableLabel(op.name)}</span>
                    <code className="finding-card__fact-value">{op.value}</code>
                    <span className="finding-card__fact-meta">
                      {readableSource(op.source)} · {readableStatus(op.status)}
                    </span>
                  </div>
                ))}
              </div>
              {finding.trace.comparison && (
                <p className="finding-card__facts-comparison">
                  Recorded comparison: <code>{finding.trace.comparison}</code>
                </p>
              )}
            </section>
          )}

          {/* Calculation trace */}
          {finding.trace && (
            <div className="finding-card__trace-section">
              <button
                className="finding-card__trace-toggle"
                onClick={() => setShowTrace(t => !t)}
                aria-expanded={showTrace}
              >
                <span>Recorded trace</span>
                <ChevronRight size={11} className="collapsible-chevron" data-open={showTrace} />
              </button>
              <div className="collapsible" data-open={showTrace} inert={!showTrace}>
                <div className="finding-card__trace">
                  <div className="finding-card__trace-op">
                    <span className="finding-card__trace-key">operation</span>
                    <span className="finding-card__trace-val">{finding.trace.operation}</span>
                  </div>
                  {finding.trace.operands.map((op, i) => (
                    <div key={i} className="finding-card__trace-op">
                      <span className="finding-card__trace-key">{op.name}</span>
                      <span className="finding-card__trace-val">{op.value}</span>
                      <span className="finding-card__trace-source">{op.source}</span>
                    </div>
                  ))}
                  <div className="finding-card__trace-comparison">
                    {finding.trace.comparison}
                  </div>
                </div>
              </div>
            </div>
          )}

          {/* Evidence + actions row */}
          <div className="finding-card__footer">
            <div className="finding-card__evidence-info">
              {finding.arch_evidence && (
                <span className="finding-card__evidence-tag">
                  Arch p.{finding.arch_evidence.page}
                </span>
              )}
              {finding.shop_evidence && (
                <span className="finding-card__evidence-tag">
                  Shop p.{finding.shop_evidence.page}
                </span>
              )}
            </div>

            <div className="finding-card__actions">
              <button
                className="btn btn--ghost btn--sm finding-card__evidence-btn"
                onClick={() => void onViewEvidence(finding)}
                aria-label="View recorded evidence and check facts"
              >
                <FileSearch size={12} />
                Evidence &amp; facts
                <ExternalLink size={10} />
              </button>

              {unresolved && !requiresRerun && finding.outcome !== 'PASS' && finding.outcome !== 'NO_APPLICABLE_RULE' && (
                <div className="finding-card__reviewer-actions" aria-busy={isSaving}>
                    <button
                      className="btn btn--reviewer"
                      disabled={isSaving}
                      onClick={() => submitAction('confirm')}
                    >{needsNote ? 'Checked: OK' : 'Confirm finding'}</button>
                  <span className="finding-card__problem-label">Problem:</span>
                  <button
                    className="btn btn--reviewer"
                    disabled={isSaving}
                    onClick={() => { setSaveError(null); setPending(pending === 'correct' ? null : 'correct'); }}
                    aria-expanded={pending === 'correct'}
                  >Correct</button>
                  <button
                    className="btn btn--reviewer"
                    disabled={isSaving}
                    onClick={() => { setSaveError(null); setPending(pending === 'except' ? null : 'except'); }}
                    aria-expanded={pending === 'except'}
                  >Exception</button>
                  <button
                    className="btn btn--reviewer btn--reviewer--dismiss"
                    disabled={isSaving}
                    onClick={() => submitAction('dismiss')}
                  >{needsNote ? 'Not checkable' : 'Dismiss'}</button>
                </div>
              )}

              {!pending && isSaving && <p role="status">Saving decision…</p>}
              {!pending && saveError && <p className="finding-card__save-error" role="alert">{saveError}</p>}

              {(pending === 'confirm' || pending === 'dismiss') && (
                <form className="finding-card__decision" aria-busy={isSaving} onSubmit={(event) => {
                  event.preventDefault();
                  if (!reason.trim()) return;
                  void saveDecision(() => onAction(finding.id, pending, reason.trim()), {
                    busy: setIsSaving, error: setSaveError,
                    saved: () => { setPending(null); setReason(''); },
                  });
                }}>
                  <label htmlFor={`note-${finding.id}`}>
                    {pending === 'confirm' ? 'What did you check?' : finding.outcome === 'FAIL' ? 'Why are you dismissing this failure?' : 'Why is this not checkable?'} (required)
                  </label>
                  <textarea id={`note-${finding.id}`} required value={reason} disabled={isSaving}
                    onChange={event => setReason(event.target.value)} rows={3} autoFocus />
                  <small>This records your decision. The check remains {finding.outcome.replaceAll('_', ' ')}.</small>
                  <button type="submit" className="btn btn--reviewer" disabled={isSaving || !reason.trim()}>
                    {isSaving ? 'Saving…' : 'Record decision'}
                  </button>
                  <button type="button" className="btn btn--ghost" disabled={isSaving} onClick={() => setPending(null)}>Cancel</button>
                  {saveError && <p role="alert">{saveError}</p>}
                </form>
              )}

              {pending === 'correct' && (
                <form
                  className="finding-card__decision"
                  aria-busy={isSaving}
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (!correctedValue.trim()) return;
                    void saveDecision(() => onCorrect(finding.id, correctedValue.trim()), {
                      busy: setIsSaving, error: setSaveError,
                      saved: () => { setPending(null); setCorrectedValue(''); },
                    });
                  }}
                >
                  <label className="finding-card__decision-label" htmlFor={`c-${finding.id}`}>
                    What should it say?
                  </label>
                  <input
                    id={`c-${finding.id}`}
                    className="value-input"
                    /* With its unit, and the server parses it: `25.5"` and `25 1/2"` are the same
                       correction. A bare number is refused rather than assumed to be inches. */
                    placeholder={'e.g. 25 1/2"'}
                    value={correctedValue}
                    disabled={isSaving}
                    aria-describedby={saveError ? `save-error-${finding.id}` : undefined}
                    onChange={(e) => setCorrectedValue(e.target.value)}
                    autoFocus
                  />
                  <button className="btn btn--reviewer" type="submit" disabled={isSaving || !correctedValue.trim()}>
                    {isSaving ? 'Saving…' : 'Record correction'}
                  </button>
                  {saveError && <p id={`save-error-${finding.id}`} className="finding-card__save-error" role="alert">{saveError}</p>}
                </form>
              )}

              {pending === 'except' && (
                <form
                  className="finding-card__decision"
                  aria-busy={isSaving}
                  onSubmit={(event) => {
                    event.preventDefault();
                    if (!reason.trim() || !expiresAt) return;
                    // Midday rather than midnight: a date input gives no time, and an exception
                    // stamped 00:00 in a browser east of UTC expires the day before it was granted.
                    void saveDecision(() => onExcept(finding.id, reason.trim(), new Date(`${expiresAt}T12:00:00Z`).toISOString()), {
                      busy: setIsSaving, error: setSaveError,
                      saved: () => { setPending(null); setReason(''); setExpiresAt(''); },
                    });
                  }}
                >
                  <label className="finding-card__decision-label" htmlFor={`r-${finding.id}`}>
                    Why is this acceptable?
                  </label>
                  <input
                    id={`r-${finding.id}`}
                    className="value-input"
                    placeholder="e.g. the vendor confirmed the site dimension by phone"
                    value={reason}
                    disabled={isSaving}
                    aria-describedby={saveError ? `save-error-${finding.id}` : undefined}
                    onChange={(e) => setReason(e.target.value)}
                    autoFocus
                  />
                  <label className="finding-card__decision-label" htmlFor={`e-${finding.id}`}>
                    Until when?
                  </label>
                  <input
                    id={`e-${finding.id}`}
                    className="value-input"
                    type="date"
                    /* Required, and there is no "never". The date is the control: it is what forces
                       somebody to look again, and the person who looks again is usually not the
                       person who granted it. */
                    value={expiresAt}
                    disabled={isSaving}
                    aria-describedby={saveError ? `save-error-${finding.id}` : undefined}
                    onChange={(e) => setExpiresAt(e.target.value)}
                  />
                  <button
                    className="btn btn--reviewer"
                    type="submit"
                    disabled={isSaving || !reason.trim() || !expiresAt}
                  >
                    {isSaving ? 'Saving…' : 'Grant until this date'}
                  </button>
                  {saveError && <p id={`save-error-${finding.id}`} className="finding-card__save-error" role="alert">{saveError}</p>}
                </form>
              )}

              {unresolved && <p className="finding-card__reason" role="status">
                {requiresRerun ? 'Correction recorded. Run checks again before reviewing the new finding or signing off.' : 'This finding still needs your decision. Add a note when required; a reading confirmation alone does not finish the review.'}
              </p>}
              {hasAction && (
                <div className="finding-card__actioned-label">
                  {!unresolved && <CheckCircle size={11} />}
                  <div><strong>{unresolved ? `Recorded action: ${finding.reviewer_action} — review not complete` : `Reviewer decision: ${needsNote && finding.reviewer_action === 'confirm' ? 'Checked: OK' : needsNote && finding.reviewer_action === 'dismiss' ? 'Not checkable' : finding.reviewer_action}`}</strong>
                    <p>{finding.reviewed_by ?? 'Reviewer recorded'}{finding.reviewed_at ? ` · ${new Date(finding.reviewed_at).toLocaleString()}` : ''}{finding.reviewer_carried_over ? ' · carried over from the previous check run' : ''}</p>
                    {finding.reviewer_note && <p>{finding.reviewer_note}</p>}
                    <small>Recorded check unchanged: {finding.outcome.replaceAll('_', ' ')}</small>
                  </div>
                </div>
              )}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
