import { X, FileText, MapPin } from 'lucide-react';
import type { Finding } from '../../data/types';
import { OutcomeBadge } from '../ui/Badge';
import './EvidencePanel.css';

interface EvidencePanelProps {
  finding: Finding;
  onClose: () => void;
}

export function EvidencePanel({ finding, onClose }: EvidencePanelProps) {
  return (
    <div className="evidence-panel animate-slide-in-r" aria-label="Evidence viewer">
      {/* Panel header — draggable to rearrange layout */}
      <div
        className="evidence-panel__header"
        draggable
        onDragStart={(e) => {
          e.dataTransfer.setData('text/plain', 'evidence-panel');
        }}
        style={{ cursor: 'grab' }}
        title="Drag drawing panel to swap column positions"
      >
        <div className="evidence-panel__title">
          <span className="evidence-panel__check-id">{finding.check_id}</span>
          <span className="evidence-panel__name">{finding.name}</span>
          <OutcomeBadge outcome={finding.outcome} size="sm" />
        </div>
        <button
          className="btn btn--subtle btn--icon btn--sm"
          onClick={onClose}
          aria-label="Close evidence panel"
        >
          <X size={14} />
        </button>
      </div>

      {/* Scrollable body */}
      <div className="evidence-panel__body">

        {/* Arch set viewer */}
        {finding.arch_evidence && (
          <PdfPane
            label="Architectural Set"
            role="ARCH"
            page={finding.arch_evidence.page}
            rawText={finding.arch_evidence.raw_text}
            extractor={finding.arch_evidence.extractor}
            outcome={finding.outcome}
            polygon={finding.arch_evidence.polygon}
          />
        )}

        {/* Shop drawing viewer */}
        {finding.shop_evidence && (
          <PdfPane
            label="Shop Drawing"
            role="SHOP"
            page={finding.shop_evidence.page}
            rawText={finding.shop_evidence.raw_text}
            extractor={finding.shop_evidence.extractor}
            outcome={finding.outcome}
            polygon={finding.shop_evidence.polygon}
          />
        )}

        {/* No evidence */}
        {!finding.arch_evidence && !finding.shop_evidence && (
          <div className="evidence-panel__no-evidence">
            <FileText size={24} className="evidence-panel__no-evidence-icon" />
            <p>No evidence located for this finding.</p>
            <p className="evidence-panel__no-evidence-sub">
              {finding.outcome === 'NOT_FOUND'
                ? 'The system searched all pages and found no matching dimension.'
                : 'Evidence was not extracted for this check type.'}
            </p>
          </div>
        )}
      </div>
    </div>
  );
}

// ── PDF Pane ─────────────────────────────────────────────────
interface PdfPaneProps {
  label: string;
  role: 'ARCH' | 'SHOP';
  page: number;
  rawText: string;
  extractor: string;
  outcome: Finding['outcome'];
  polygon: [number, number][];
}

function PdfPane({ label, role, page, rawText, extractor, outcome, polygon }: PdfPaneProps) {
  const borderColor =
    outcome === 'FAIL'            ? 'var(--status-fail)' :
    outcome === 'REVIEW_REQUIRED' ? 'var(--status-review)' :
    outcome === 'PASS'            ? 'var(--status-pass)' :
    'var(--status-missing)';

  // Compute bounding box from polygon for the overlay div
  const xs = polygon.map(p => p[0]);
  const ys = polygon.map(p => p[1]);
  const x = Math.min(...xs);
  const y = Math.min(...ys);
  const w = Math.max(...xs) - x;
  const h = Math.max(...ys) - y;

  return (
    <div className="pdf-pane">
      <div className="pdf-pane__header">
        <div className="pdf-pane__label-row">
          <div className={`pdf-pane__role-tag pdf-pane__role-tag--${role.toLowerCase()}`}>
            {role}
          </div>
          <span className="pdf-pane__label">{label}</span>
          {/* The filename used to be a fixed string here — two of them, naming documents
              nobody uploaded. A finding does not carry one, so nothing is shown. */}
        </div>
        <span className="pdf-pane__page">
          <MapPin size={11} />
          Page {page}
        </span>
      </div>

      {/* No page image. The overlay used to sit on a hand-drawn SVG of a title block reading
          "MARRIOTT HOUSTON / PKG-2026-001" with a dimension of 6012 on it — a drawing that does not
          exist, under a highlight box positioned from the finding's real polygon. A reviewer would
          have been looking at genuine coordinates over invented paper and signing off on the result.

          The page cannot be rendered yet: nothing serves document bytes back, so there is no image to
          put here. Until there is, the panel shows the located region as numbers, which is true, and
          says plainly that it is not showing the drawing. */}
      <div className="pdf-pane__meta">
        <span className="pdf-pane__meta-label">Located at</span>
        <code className="pdf-pane__raw-value">
          page {page} · x {Math.round(x)}–{Math.round(x + w)} · y {Math.round(y)}–{Math.round(y + h)}
        </code>
        <span className="pdf-pane__extractor" style={{ color: borderColor }}>
          The drawing itself is not shown — the page image is not available to this app yet.
        </span>
      </div>

      {/* Raw text readout */}
      <div className="pdf-pane__meta">
        <div className="pdf-pane__raw-text">
          <span className="pdf-pane__meta-label">Extracted text</span>
          <code className="pdf-pane__raw-value">"{rawText}"</code>
        </div>
        <span className="pdf-pane__extractor">via {extractor}</span>
      </div>
    </div>
  );
}
