/**
 * The findings of one answer as a real table, not a markdown string.
 *
 * It replaces a round trip that flattened structured findings into markdown and parsed them back:
 * mid-reveal that showed bare `|` characters, and the table repeated every fact the card list
 * below it already showed. Here each finding is one row; its narrative and its actions open under
 * the row on demand, so the default view is the table and nothing else.
 *
 * **What is highlighted, and why.** A reviewer's eye should land on what needs them. Rows are
 * ordered by what they ask of the reviewer (fail, review, not found, pass) and carry a status
 * edge; in a FAIL row the vendor's reading is highlighted, because that is the value that is
 * wrong. Nothing else is coloured.
 *
 * Every cell, including the outcome label, comes from `findingCells`, the function the markdown
 * table uses. The two therefore show the same values and the same outcome wording. No cell is
 * written by a model.
 *
 * **An opened row stays mounted.** Closing a row hides its detail rather than removing it, so a
 * correction or exception a reviewer has started typing is still there when the row reopens.
 */

import { useState, type ReactNode } from 'react';
import { AlertCircle, CheckCircle2, ChevronRight, CircleDashed, Eye, MinusCircle, XCircle } from 'lucide-react';
import type { Finding, Outcome } from '../../data/types';
import { findingCells } from '../chat/findingsTable.js';
import { closedRows, sortFindings, toggleRow, type RowState } from './findingsOrder.js';

const ICON: Record<Outcome, ReactNode> = {
  FAIL: <XCircle size={13} aria-hidden="true" />,
  REVIEW_REQUIRED: <AlertCircle size={13} aria-hidden="true" />,
  NOT_FOUND: <CircleDashed size={13} aria-hidden="true" />,
  PASS: <CheckCircle2 size={13} aria-hidden="true" />,
  NO_APPLICABLE_RULE: <MinusCircle size={13} aria-hidden="true" />,
};

const NOT_RECORDED = 'Not recorded';

interface FindingsTableProps {
  findings: readonly Finding[];
  /** The guarded narrative per finding id. Shown only when a row is opened. */
  narratives?: Readonly<Record<string, string>>;
  selectedFinding?: string | null;
  onViewEvidence?: (finding: Finding) => void;
  /** Rendered under an opened row: the existing card with evidence and reviewer actions. */
  renderDetail?: (finding: Finding) => ReactNode;
  /** Rows open on first render. For tests and for deep links; the default is all closed. */
  initiallyOpen?: readonly string[];
}

export function FindingsTable({
  findings,
  narratives,
  selectedFinding,
  onViewEvidence,
  renderDetail,
  initiallyOpen = [],
}: FindingsTableProps) {
  const [rows, setRows] = useState<RowState>(() => closedRows(initiallyOpen));
  if (findings.length === 0) return null;

  return (
    <div className="ftable-wrap">
      <table className="ftable">
        <thead>
          <tr>
            <th scope="col" className="ftable__col-outcome">Result</th>
            <th scope="col">Check</th>
            <th scope="col">
              <span className="ftable__role ftable__role--shop" aria-hidden="true" />
              Shop
            </th>
            <th scope="col">
              <span className="ftable__role ftable__role--arch" aria-hidden="true" />
              Arch
            </th>
            <th scope="col">Sheet</th>
            <th scope="col" className="ftable__col-actions">
              <span className="sr-only">Actions</span>
            </th>
          </tr>
        </thead>
        <tbody>
          {sortFindings(findings).map((finding) => (
            <FindingRows
              key={finding.id}
              finding={finding}
              isOpen={rows.open.has(finding.id)}
              isMounted={rows.mounted.has(finding.id)}
              isSelected={selectedFinding === finding.id}
              narrative={narratives?.[finding.id]}
              onToggle={() => setRows((current) => toggleRow(current, finding.id))}
              onViewEvidence={onViewEvidence}
              renderDetail={renderDetail}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface FindingRowsProps {
  finding: Finding;
  isOpen: boolean;
  isMounted: boolean;
  isSelected: boolean;
  narrative?: string;
  onToggle: () => void;
  onViewEvidence?: (finding: Finding) => void;
  renderDetail?: (finding: Finding) => ReactNode;
}

function FindingRows({
  finding,
  isOpen,
  isMounted,
  isSelected,
  narrative,
  onToggle,
  onViewEvidence,
  renderDetail,
}: FindingRowsProps) {
  const cells = findingCells(finding);
  const detailId = `ftable-detail-${finding.id}`;
  // The chain supplies the values. `recorded_operands` is undefined when that request did not
  // complete, which is not the same as the chain recording no value.
  const loaded = finding.recorded_operands !== undefined;
  const detail = isMounted && renderDetail ? renderDetail(finding) : null;

  return (
    <>
      <tr
        className="ftable__row"
        data-outcome={finding.outcome}
        data-selected={isSelected || undefined}
        data-open={isOpen || undefined}
      >
        <td>
          <span className="ftable__chip" data-outcome={finding.outcome}>
            {ICON[finding.outcome]}
            {cells.outcome}
          </span>
        </td>
        <th scope="row" className="ftable__check">
          {cells.check}
          <span className="ftable__rule mono">{finding.check_id}</span>
          <span className="ftable__rule">{finding.scope_label || 'Package revision'}</span>
        </th>
        <td className="mono ftable__value ftable__value--shop" data-mismatch={finding.outcome === 'FAIL' || undefined}>
          <Value text={cells.reading} loaded={loaded} />
        </td>
        <td className="mono ftable__value">
          <Value text={cells.comparison} loaded={loaded} />
        </td>
        <td className="mono ftable__sheet">
          <Value text={cells.sheet} loaded={loaded} />
        </td>
        <td className="ftable__actions">
          {onViewEvidence && (finding.shop_evidence || finding.arch_evidence) && (
            <button
              type="button"
              className="ftable__icon-btn"
              onClick={() => onViewEvidence(finding)}
              aria-label={`View evidence for ${cells.check}`}
              title="View evidence"
            >
              <Eye size={14} aria-hidden="true" />
            </button>
          )}
          <button
            type="button"
            className="ftable__icon-btn ftable__expand"
            onClick={onToggle}
            aria-expanded={isOpen}
            aria-controls={isMounted ? detailId : undefined}
            aria-label={`${isOpen ? 'Hide' : 'Show'} details for ${cells.check}`}
            title={isOpen ? 'Hide details' : 'Details and actions'}
          >
            <ChevronRight size={14} aria-hidden="true" />
          </button>
        </td>
      </tr>
      {isMounted && (
        <tr className="ftable__detail" id={detailId} data-outcome={finding.outcome} hidden={!isOpen}>
          <td colSpan={6}>
            {narrative && <p className="ftable__narrative">{narrative}</p>}
            {detail}
            {!narrative && detail === null && (
              <p className="ftable__narrative ftable__narrative--empty">No explanation for this check.</p>
            )}
          </td>
        </tr>
      )}
    </>
  );
}

/**
 * A missing value is a dash, not the words repeated down a column. Its accessible name says which
 * kind of missing it is: not recorded by the chain, or not loaded because the request failed.
 */
function Value({ text, loaded }: { text: string; loaded: boolean }) {
  if (!text || text === NOT_RECORDED) {
    const label = loaded ? NOT_RECORDED : 'Not loaded';
    return (
      <span className="ftable__empty" data-loaded={loaded} title={label} aria-label={label}>
        —
      </span>
    );
  }
  return <>{text}</>;
}
