import { useState, useEffect } from 'react';
import { ChevronDown, ChevronRight, FileSearch, ExternalLink, CheckCircle, XCircle, AlertTriangle, MinusCircle, TriangleAlert } from 'lucide-react';
import type { Finding } from '../../data/mock';
import { OutcomeBadge, SeverityDot } from '../ui/Badge';
import './FindingCard.css';

// ── Outcome icon map — audit #7: HelpCircle→MinusCircle (neutral, not alarming)
const OUTCOME_ICON = {
  PASS:               CheckCircle,
  FAIL:               XCircle,
  REVIEW_REQUIRED:    AlertTriangle,
  NOT_FOUND:          MinusCircle,
  NO_APPLICABLE_RULE: MinusCircle,
};

interface FindingCardProps {
  finding: Finding;
  isSelected: boolean;
  onViewEvidence: (finding: Finding) => void;
  onAction: (findingId: string, action: 'confirm' | 'correct' | 'except' | 'dismiss', note?: string) => void;
  animationDelay?: number;
}

export function FindingCard({
  finding,
  isSelected,
  onViewEvidence,
  onAction,
  animationDelay = 0,
}: FindingCardProps) {
  const [expanded, setExpanded] = useState(finding.outcome === 'FAIL');
  const [showTrace, setShowTrace] = useState(false);

  useEffect(() => {
    if (isSelected) {
      setExpanded(true);
    }
  }, [isSelected]);

  const Icon = OUTCOME_ICON[finding.outcome];
  const hasEvidence = finding.arch_evidence || finding.shop_evidence;
  const hasAction = finding.reviewer_action !== null;

  return (
    <div
      className={`finding-card finding-card--${finding.outcome.toLowerCase().replace('_', '-')} ${isSelected ? 'finding-card--selected' : ''} ${hasAction ? 'finding-card--actioned' : ''} animate-slide-up`}
      style={{ animationDelay: `${animationDelay}ms` }}
      aria-label={`${finding.check_id}: ${finding.name} — ${finding.outcome}`}
    >
      {/* ── Header row ──────────────────────────────────── */}
      <button
        className="finding-card__header"
        onClick={() => setExpanded(e => !e)}
        aria-expanded={expanded}
      >
        <div className="finding-card__header-left">
          <Icon size={14} className="finding-card__outcome-icon" />
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
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </div>
      </button>

      {/* ── Expanded body ────────────────────────────────── */}
      {expanded && (
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

          {/* Calculation trace */}
          {finding.trace && (
            <div className="finding-card__trace-section">
              <button
                className="finding-card__trace-toggle"
                onClick={() => setShowTrace(t => !t)}
              >
                <span>Calculation trace</span>
                {showTrace ? <ChevronDown size={11} /> : <ChevronRight size={11} />}
              </button>
              {showTrace && (
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
              )}
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
              {hasEvidence && (
                <button
                  className="btn btn--ghost btn--sm finding-card__evidence-btn"
                  onClick={() => onViewEvidence(finding)}
                  aria-label="View evidence in PDFs"
                >
                  <FileSearch size={12} />
                  View Evidence
                  <ExternalLink size={10} />
                </button>
              )}

              {!hasAction && finding.outcome !== 'PASS' && finding.outcome !== 'NO_APPLICABLE_RULE' && (
                <div className="finding-card__reviewer-actions">
                  {finding.outcome !== 'NOT_FOUND' && (
                    <button
                      className="btn btn--reviewer"
                      onClick={() => onAction(finding.id, 'confirm')}
                    >Confirm</button>
                  )}
                  <button
                    className="btn btn--reviewer"
                    onClick={() => onAction(finding.id, 'correct')}
                  >Correct</button>
                  <button
                    className="btn btn--reviewer"
                    onClick={() => onAction(finding.id, 'except')}
                  >Exception</button>
                  <button
                    className="btn btn--reviewer btn--reviewer--dismiss"
                    onClick={() => onAction(finding.id, 'dismiss')}
                  >Dismiss</button>
                </div>
              )}

              {hasAction && (
                <span className="finding-card__actioned-label">
                  <CheckCircle size={11} />
                  Reviewer: {finding.reviewer_action}
                </span>
              )}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
