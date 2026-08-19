import { X, FileText, MapPin } from 'lucide-react';
import type { Finding } from '../../data/mock';
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
            filename="GV_Arch_Set_Marriott_Houston_R4.pdf"
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
            filename="EliteStone_ShopDrawing_V2.pdf"
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
  filename: string;
  page: number;
  rawText: string;
  extractor: string;
  outcome: Finding['outcome'];
  polygon: [number, number][];
}

function PdfPane({ label, role, filename, page, rawText, extractor, outcome, polygon }: PdfPaneProps) {
  const overlayColor =
    outcome === 'FAIL'            ? 'var(--evidence-fail)' :
    outcome === 'REVIEW_REQUIRED' ? 'var(--evidence-review)' :
    outcome === 'PASS'            ? 'var(--evidence-pass)' :
    'var(--bg-hover)';

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
          <span className="pdf-pane__filename truncate" style={{ fontSize: '11px', color: 'var(--text-muted)', marginLeft: 'var(--space-2)', maxWidth: '140px' }} title={filename}>
            {filename}
          </span>
        </div>
        <span className="pdf-pane__page">
          <MapPin size={11} />
          Page {page}
        </span>
      </div>

      {/* Simulated PDF canvas */}
      <div className="pdf-pane__canvas" aria-label={`${label} page ${page}`}>
        {/* PDF placeholder — would be PDF.js in production */}
        <div className="pdf-pane__placeholder">
          {/* Fake dimension lines */}
          <PdfSimulation polygon={polygon} overlayColor={overlayColor} borderColor={borderColor} />
        </div>

        {/* Evidence highlight overlay */}
        <div
          className="pdf-pane__overlay"
          aria-hidden="true"
          style={{
            left: `${(x / 560) * 100}%`,
            top:  `${(y / 700) * 100}%`,
            width: `${(w / 560) * 100}%`,
            height: `${(h / 700) * 100}%`,
            background: overlayColor,
            borderColor,
          }}
        />
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

// ── Simulated PDF background ──────────────────────────────────
function PdfSimulation({ polygon, overlayColor, borderColor }: {
  polygon: [number, number][];
  overlayColor: string;
  borderColor: string;
}) {
  return (
    <svg viewBox="0 0 560 700" className="pdf-pane__svg" aria-hidden="true">
      {/* Page background */}
      <rect width="560" height="700" fill="transparent" />

      {/* Fake title block */}
      <rect x="20" y="20" width="520" height="40" rx="2" fill="var(--pdf-footer)" />
      <text x="40" y="46" fontSize="12" fill="var(--text-muted)" fontFamily="monospace">GV SHOP DRAWING — MARRIOTT HOUSTON</text>
      <rect x="400" y="22" width="140" height="36" rx="1" fill="var(--border-default)" />
      <text x="415" y="40" fontSize="9" fill="var(--text-muted)" fontFamily="monospace">PKG-2026-001</text>
      <text x="415" y="52" fontSize="9" fill="var(--text-muted)" fontFamily="monospace">Rev 2 — 2026-08-15</text>

      {/* Fake dimension lines + lines */}
      <line x1="80" y1="200" x2="480" y2="200" stroke="var(--pdf-lines)" strokeWidth="0.5" />
      <line x1="80" y1="200" x2="80" y2="220" stroke="var(--pdf-lines)" strokeWidth="0.5" />
      <line x1="480" y1="200" x2="480" y2="220" stroke="var(--pdf-lines)" strokeWidth="0.5" />
      <line x1="80" y1="300" x2="480" y2="300" stroke="var(--pdf-lines)" strokeWidth="1" />
      <line x1="80" y1="300" x2="80" y2="420" stroke="var(--pdf-lines)" strokeWidth="1" />
      <line x1="480" y1="300" x2="480" y2="420" stroke="var(--pdf-lines)" strokeWidth="1" />
      <line x1="80" y1="420" x2="480" y2="420" stroke="var(--pdf-lines)" strokeWidth="1" />
      <line x1="100" y1="330" x2="460" y2="330" stroke="var(--pdf-lines)" strokeWidth="0.5" strokeDasharray="4,4" />
      <line x1="100" y1="390" x2="460" y2="390" stroke="var(--pdf-lines)" strokeWidth="0.5" strokeDasharray="4,4" />

      {/* Highlighted region */}
      <rect
        x={Math.min(...polygon.map(p => p[0]))}
        y={Math.min(...polygon.map(p => p[1]))}
        width={Math.max(...polygon.map(p => p[0])) - Math.min(...polygon.map(p => p[0]))}
        height={Math.max(...polygon.map(p => p[1])) - Math.min(...polygon.map(p => p[1]))}
        fill={overlayColor}
        stroke={borderColor}
        strokeWidth="1.5"
        rx="2"
        opacity="0.9"
      />

      {/* Fake text labels */}
      <text x="260" y="196" fontSize="9" fill="var(--text-muted)" textAnchor="middle" fontFamily="monospace">dimension line</text>
      <text x="260" y="352" fontSize="10" fill="var(--text-primary)" textAnchor="middle" fontFamily="monospace" fontWeight="bold">6012 [236 3/4]</text>
      <text x="260" y="395" fontSize="9" fill="var(--text-body)" textAnchor="middle" fontFamily="monospace">610 [24]</text>

      {/* Bottom border */}
      <rect x="0" y="660" width="560" height="40" fill="var(--pdf-footer)" />
      <text x="280" y="682" fontSize="8" fill="var(--text-muted)" textAnchor="middle" fontFamily="monospace">GRANITI VICENTIA — CONFIDENTIAL SHOP DRAWING</text>
    </svg>
  );
}
