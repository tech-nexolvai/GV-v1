import { useState } from 'react';
import type { Finding } from '../../data/types';
import type { ApprovalReadiness, SlotReaderRow } from '../../api/client';
import { FindingCard } from '../chat/FindingCard';
import { OutcomeIcon } from '../ui/OutcomeIcon';
import type { DecisionSaveResult, SimpleReviewAction } from '../chat/decisionSave';
import { resultGroups } from './reviewerResults';
import './ResultsPanel.css';

const RESULT_LABELS = {
  PASS: 'PASS', FAIL: 'FAIL', REVIEW_REQUIRED: 'Needs you',
  NOT_FOUND: 'Not checkable', NO_APPLICABLE_RULE: 'No applicable rule',
};

export function ResultsPanel({ findings, rows, readiness, selected, busy, onRefresh, onShowDrawing, onOpenRow, onViewEvidence, onAction, onCorrect, onExcept }: {
  findings: Finding[]; rows: SlotReaderRow[]; readiness: ApprovalReadiness | null; selected: string | null; busy: boolean;
  onRefresh: () => void; onShowDrawing: (finding: Finding) => void; onOpenRow: (rowId: string) => void;
  onViewEvidence: (finding: Finding) => void;
  onAction: (id: string, action: SimpleReviewAction, note?: string) => Promise<DecisionSaveResult>;
  onCorrect: (id: string, value: string) => Promise<DecisionSaveResult>;
  onExcept: (id: string, reason: string, expires: string) => Promise<DecisionSaveResult>;
}) {
  const [onlyNeedsMe, setOnlyNeedsMe] = useState(true);
  const blockers = new Set(readiness?.blocking_finding_ids ?? []);
  const visible = onlyNeedsMe && readiness ? findings.filter(f => blockers.has(f.id)) : findings;
  return <section className="results-panel" aria-labelledby="results-title" aria-busy={busy}>
    <header className="results-panel__header">
      <div><h2 id="results-title">Results</h2><p>{readiness ? `${readiness.blocking_findings} need you` : 'Checking what needs your review…'} · {findings.length} recorded checks</p></div>
      <button type="button" className="btn btn--subtle" disabled={busy} onClick={onRefresh}>{busy ? 'Refreshing…' : 'Refresh results'}</button>
    </header>
    <label className="results-panel__filter"><input type="checkbox" checked={onlyNeedsMe} onChange={e => setOnlyNeedsMe(e.target.checked)} />Only what needs me</label>
    {findings.length === 0 ? <p>No recorded checks yet. Save the measurements and run checks.</p> : visible.length === 0 ? <p>Every recorded finding has been addressed. Uncheck the filter to see all results.</p> : resultGroups(visible).map(group => <section key={group.page ?? 'other'} className="results-panel__page">
      <h3>{group.page === null ? 'Other recorded checks' : `Page ${group.page}`}</h3>
      {group.items.map(finding => {
        const row = rows.find(r => r.row_id === finding.scope_row_candidate_id);
        return <article key={finding.id} id={`result-${finding.id}`} className="results-panel__result" tabIndex={-1}>
          <div className="results-panel__identity"><h4>{finding.scope_label ?? finding.name}</h4>
            <span className="results-panel__outcome" title={`Recorded check: ${finding.outcome}`}><OutcomeIcon outcome={finding.outcome} size={16} />{RESULT_LABELS[finding.outcome]}</span>
          </div>
          <p className="results-panel__attribution">{finding.reviewer_action ? 'Reviewer decision recorded separately below' : 'Recorded check · no reviewer decision'}</p>
          {row && <details className="results-panel__row-values"><summary>Current row values and wall layout</summary>
            <p>Saved row state. The recorded check below changes only when checks run again.</p>
            <dl>{row.values.map(value => <div key={value.key}><dt>{value.label}</dt><dd>{value.value ?? 'Needs a value'}{value.source ? ` · ${value.source}` : ''}</dd></div>)}
              <div><dt>Wall layout</dt><dd>{row.wall_config ? row.wall_config.replaceAll('_', ' ') : row.wall_source === 'vendor-drawing-clues' ? row.wall_proposal?.replaceAll('_', ' ') : 'Not confirmed'} · {row.confirmed_by ? `reviewer ${row.confirmed_by}` : row.wall_source ?? 'not recorded'}</dd></div>
            </dl>
          </details>}
          <div className="results-panel__links">
            <button type="button" className="btn btn--subtle" disabled={!finding.row_location && !finding.shop_evidence && !finding.arch_evidence} onClick={() => onShowDrawing(finding)}>Show on drawing</button>
            {finding.scope_row_candidate_id && <button type="button" className="btn btn--ghost" onClick={() => onOpenRow(finding.scope_row_candidate_id!)}>Open countertop card</button>}
            {!finding.row_location && !finding.shop_evidence && !finding.arch_evidence && <small>No stored drawing location for this check.</small>}
          </div>
          <FindingCard finding={finding} defaultExpanded isSelected={selected === finding.id} onViewEvidence={onViewEvidence} onAction={onAction} onCorrect={onCorrect} onExcept={onExcept} />
        </article>;
      })}
    </section>)}
  </section>;
}
