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
 * Every cell comes from `findingCells`, the same function the markdown table uses, so the two
 * can never disagree. No cell is written by a model.
 */

import { useState, type ReactNode } from 'react';
import { AlertCircle, CheckCircle2, ChevronRight, CircleDashed, Eye, MinusCircle, XCircle } from 'lucide-react';
import type { Finding, Outcome } from '../../data/types';
import { findingCells } from '../chat/findingsTable.js';
import { sortFindings } from './findingsOrder.js';

const CHIP: Record<Outcome, { label: string; icon: ReactNode }> = {
  FAIL: { label: 'Fail', icon: <XCircle size={13} aria-hidden="true" /> },
  REVIEW_REQUIRED: { label: 'Review', icon: <AlertCircle size={13} aria-hidden="true" /> },
  NOT_FOUND: { label: 'Not found', icon: <CircleDashed size={13} aria-hidden="true" /> },
  PASS: { label: 'Pass', icon: <CheckCircle2 size={13} aria-hidden="true" /> },
  NO_APPLICABLE_RULE: { label: 'N/A', icon: <MinusCircle size={13} aria-hidden="true" /> },
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
  const [open, setOpen] = useState<ReadonlySet<string>>(() => new Set(initiallyOpen));
  if (findings.length === 0) return null;

  const toggle = (id: string) =>
    setOpen((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

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
          {sortFindings(findings).map((finding) => {
            const cells = findingCells(finding);
            const isOpen = open.has(finding.id);
            const detailId = `ftable-detail-${finding.id}`;
            const narrative = narratives?.[finding.id];
            const chip = CHIP[finding.outcome];
            return (
              <FindingRows
                key={finding.id}
                finding={finding}
                cells={cells}
                chip={chip}
                isOpen={isOpen}
                isSelected={selectedFinding === finding.id}
                detailId={detailId}
                narrative={narrative}
                onToggle={() => toggle(finding.id)}
                onViewEvidence={onViewEvidence}
                detail={isOpen && renderDetail ? renderDetail(finding) : null}
              />
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

interface FindingRowsProps {
  finding: Finding;
  cells: ReturnType<typeof findingCells>;
  chip: { label: string; icon: ReactNode };
  isOpen: boolean;
  isSelected: boolean;
  detailId: string;
  narrative?: string;
  onToggle: () => void;
  onViewEvidence?: (finding: Finding) => void;
  detail: ReactNode;
}

function FindingRows({
  finding,
  cells,
  chip,
  isOpen,
  isSelected,
  detailId,
  narrative,
  onToggle,
  onViewEvidence,
  detail,
}: FindingRowsProps) {
  return (
    <>
      <tr
        className="ftable__row"
        data-outcome={finding.outcome}
        data-selected={isSelected || undefined}
        data-open={isOpen || undefined}
      >
        <td>
          <span className="ftable__chip" data-outcome={finding.outcome} title={cells.outcome}>
            {chip.icon}
            {chip.label}
          </span>
        </td>
        <th scope="row" className="ftable__check">
          {cells.check}
          <span className="ftable__rule mono">{finding.check_id}</span>
        </th>
        <td className="mono ftable__value ftable__value--shop" data-mismatch={finding.outcome === 'FAIL' || undefined}>
          <Value text={cells.reading} />
        </td>
        <td className="mono ftable__value">
          <Value text={cells.comparison} />
        </td>
        <td className="mono ftable__sheet">
          <Value text={cells.sheet} />
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
            aria-controls={detailId}
            aria-label={`${isOpen ? 'Hide' : 'Show'} details for ${cells.check}`}
            title={isOpen ? 'Hide details' : 'Details and actions'}
          >
            <ChevronRight size={14} aria-hidden="true" />
          </button>
        </td>
      </tr>
      {isOpen && (
        <tr className="ftable__detail" id={detailId} data-outcome={finding.outcome}>
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

/** A missing value is a dash, not the words "Not recorded" repeated down a column. */
function Value({ text }: { text: string }) {
  if (!text || text === NOT_RECORDED) {
    return (
      <span className="ftable__empty" title={NOT_RECORDED} aria-label={NOT_RECORDED}>
        —
      </span>
    );
  }
  return <>{text}</>;
}
