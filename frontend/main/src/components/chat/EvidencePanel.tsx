import { useEffect, useState } from 'react';
import { ArrowLeft, X, FileImage, MapPin } from 'lucide-react';
import type { Finding } from '../../data/types';
import { downloadEvidenceCrop } from '../../api/client';
import { OutcomeBadge } from '../ui/Badge';
import './EvidencePanel.css';

interface EvidencePanelProps {
  finding: Finding;
  projectId: string;
  packageId: string;
  loading?: boolean;
  error?: string;
  onClose: () => void;
}

export function EvidencePanel({ finding, projectId, packageId, loading = false, error, onClose }: EvidencePanelProps) {
  const recordedOperands = finding.recorded_operands ?? [];
  const hasMappedCrop = Boolean(finding.arch_evidence || finding.shop_evidence);
  const hasUnmappedStoredCrop = !hasMappedCrop && recordedOperands.some(
    (operand) => operand.hasEvidence && operand.source !== 'ARCH' && operand.source !== 'SHOP',
  );
  const hasDrawingOperandWithoutCrop = recordedOperands.some(
    (operand) => (operand.source === 'ARCH' || operand.source === 'SHOP') && !operand.hasEvidence,
  );

  const sourceSummary = recordedOperands.length === 0
    ? null
    : recordedOperands
      .map((operand) =>
        `${operand.name}: ${operand.value} (${operand.source}) · ${operand.status}${operand.hasEvidence ? ' · has stored crop' : ''}`,
      )
      .join('\n');

  const noEvidenceMessage = () => {
    if (hasUnmappedStoredCrop) {
      return 'A stored crop is recorded for this finding, but its document role is not recognised by this view. The image is withheld until that evidence link is repaired.';
    }

    if (recordedOperands.length > 0) {
      if (hasDrawingOperandWithoutCrop) {
        return 'This finding uses a drawing-backed reading, but no mechanical crop was stored with it. No substitute image is shown.';
      }
      return 'This finding currently relies on non-drawing operands (for example, user input or intermediate values), so no drawing-backed crop is expected here.';
    }

    if (finding.outcome === 'REVIEW_REQUIRED') {
      return 'This is a reviewer decision (such as a layout choice), not a located drawing measurement. There is no crop to show for it.';
    }

    if (finding.outcome === 'NOT_FOUND') {
      return 'No reading was located for this check in the current run.';
    }

    return 'No stored operands are available for this finding.';
  };
  return (
    <div className="evidence-panel animate-slide-in-r" aria-label="Evidence viewer">
      <div
        className="evidence-panel__header"
      >
        <div className="evidence-panel__title">
          <span className="evidence-panel__check-id">{finding.check_id}</span>
          <span className="evidence-panel__name">{finding.name}</span>
          <OutcomeBadge outcome={finding.outcome} size="sm" />
        </div>
        <button
          className="btn btn--ghost btn--sm evidence-panel__back"
          onClick={onClose}
          aria-label="Back to findings"
        >
          <ArrowLeft size={13} />
          Findings
        </button>
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

        {loading && (
          <div className="evidence-panel__state" role="status">
            Loading the finding's recorded evidence…
          </div>
        )}

        {error && (
          <div className="evidence-panel__state evidence-panel__state--error" role="alert">
            {error}
          </div>
        )}

        {/* Arch set viewer */}
        {!loading && !error && finding.arch_evidence && (
          <PdfPane
            key={finding.arch_evidence.canonical_observation_id}
            label="Architectural Set"
            role="ARCH"
            evidence={finding.arch_evidence}
            projectId={projectId}
            packageId={packageId}
          />
        )}

        {/* Shop drawing viewer */}
        {!loading && !error && finding.shop_evidence && (
          <PdfPane
            key={finding.shop_evidence.canonical_observation_id}
            label="Shop Drawing"
            role="SHOP"
            evidence={finding.shop_evidence}
            projectId={projectId}
            packageId={packageId}
          />
        )}

        {/* No crop is intentionally not rendered as a plausible stand-in. A reviewer must be able to
            distinguish a rule result from visual drawing evidence. */}
        {!loading && !error && !finding.arch_evidence && !finding.shop_evidence && (
          <div className="evidence-panel__no-evidence">
            <FileImage size={24} className="evidence-panel__no-evidence-icon" />
            <p>No drawing crop is available for this check.</p>
            <p className="evidence-panel__no-evidence-sub">
              {noEvidenceMessage()}
            </p>
            {sourceSummary !== null && (
              <>
                <p className="evidence-panel__no-evidence-ops">
                  Recorded operands:
                </p>
                <code className="evidence-panel__no-evidence-code">{sourceSummary}</code>
              </>
            )}
            {hasUnmappedStoredCrop && (
              <p className="evidence-panel__no-evidence-guidance">
                The evidence record is retained in the finding chain; this is a linkage issue, not a verdict or value change.
              </p>
            )}
            <ul className="evidence-panel__no-evidence-guide-list">
              <li>
                {finding.outcome === 'REVIEW_REQUIRED'
                  ? 'REVIEW REQUIRED is normal: it is waiting for a reviewer choice, not a missing image.'
                  : 'A drawing crop appears only when a confirmed drawing reading has a stored location.'}
              </li>
              <li>Findings built from input-only values are intentionally shown without crops.</li>
            </ul>
            <button className="btn btn--action evidence-panel__empty-back" onClick={onClose}>
              <ArrowLeft size={13} />
              Back to findings
            </button>
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
  evidence: NonNullable<Finding['arch_evidence']>;
  projectId: string;
  packageId: string;
}

function PdfPane({ label, role, evidence, projectId, packageId }: PdfPaneProps) {
  const [crop, setCrop] = useState<{ status: 'loading' } | { status: 'ready'; url: string } | { status: 'error'; message: string }>({ status: 'loading' });

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    void downloadEvidenceCrop(projectId, packageId, evidence.canonical_observation_id).then(
      (blob) => {
        objectUrl = URL.createObjectURL(blob);
        if (active) {
          setCrop({ status: 'ready', url: objectUrl });
        } else {
          URL.revokeObjectURL(objectUrl);
        }
      },
      (cause: unknown) => {
        if (active) {
          setCrop({
            status: 'error',
            message: cause instanceof Error ? cause.message : 'The stored crop could not be loaded.',
          });
        }
      },
    );
    return () => {
      active = false;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [evidence.canonical_observation_id, packageId, projectId]);

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
          Page {evidence.page}
        </span>
      </div>

      <div className="pdf-pane__crop">
        {crop.status === 'loading' && <p className="pdf-pane__crop-state">Loading stored crop…</p>}
        {crop.status === 'ready' && (
          <img
            className="pdf-pane__crop-image"
            src={crop.url}
            alt={`Mechanical evidence crop for ${evidence.semantic_type} on page ${evidence.page}`}
          />
        )}
        {crop.status === 'error' && <p className="pdf-pane__crop-state pdf-pane__crop-state--error">{crop.message}</p>}
      </div>

      <div className="pdf-pane__meta">
        <span className="pdf-pane__meta-label">Mechanical evidence crop</span>
        <span className="pdf-pane__extractor">
          Pixel region only — not a full drawing, placement claim, or redline.
        </span>
      </div>
    </div>
  );
}
