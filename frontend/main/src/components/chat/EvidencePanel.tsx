import { useEffect, useState } from 'react';
import { ArrowLeft, X, MapPin } from 'lucide-react';
import type { Finding } from '../../data/types';
import { downloadEvidenceCrop } from '../../api/client';
import { OutcomeBadge } from '../ui/StatusBadge';
import { NoCrops } from '../drawing/evidence-crops';
import './EvidencePanel.css';

interface EvidencePanelProps {
  finding: Finding;
  projectId: string;
  packageId: string;
  loading?: boolean;
  error?: string;
  onClose: () => void;
  onShowDrawing?: () => void;
}

export function EvidencePanel({ finding, projectId, packageId, loading = false, error, onClose, onShowDrawing }: EvidencePanelProps) {
  const recordedOperands = finding.recorded_operands ?? [];
  const hasMappedCrop = Boolean(finding.arch_evidence || finding.shop_evidence);
  const hasUnmappedStoredCrop = !hasMappedCrop && recordedOperands.some(
    (operand) => operand.hasEvidence && operand.source !== 'ARCH' && operand.source !== 'SHOP',
  );
  const hasDrawingOperandWithoutCrop = recordedOperands.some(
    (operand) => (operand.source === 'ARCH' || operand.source === 'SHOP') && !operand.hasEvidence,
  );

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
        {onShowDrawing && (finding.row_location || finding.shop_evidence || finding.arch_evidence) && <button className="btn btn--subtle" onClick={onShowDrawing}>Show on drawing</button>}

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
          <CropPane
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
          <CropPane
            key={finding.shop_evidence.canonical_observation_id}
            label="Shop Drawing"
            role="SHOP"
            evidence={finding.shop_evidence}
            projectId={projectId}
            packageId={packageId}
          />
        )}

        {/* No crop is intentionally not rendered as a plausible stand-in. A reviewer must be able to
            distinguish a rule result from visual drawing evidence. One line; the reason is behind "?" (#1045). */}
        {!loading && !error && !finding.arch_evidence && !finding.shop_evidence && (
          <div className="evidence-panel__no-evidence">
            <NoCrops line="No drawing crop for this check" why={noEvidenceMessage()} />
          </div>
        )}
      </div>
    </div>
  );
}

// ── Stored mechanical crop ──────────────────────────────────
interface CropPaneProps {
  label: string;
  role: 'ARCH' | 'SHOP';
  evidence: NonNullable<Finding['arch_evidence']>;
  projectId: string;
  packageId: string;
}

function CropPane({ label, role, evidence, projectId, packageId }: CropPaneProps) {
  const [cropUrl, setCropUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    
    // The crop endpoint verifies the stored digest. Do not substitute a full drawing or pass
    // image bytes into a PDF viewer: that can display a blank frame as if evidence were present.
    downloadEvidenceCrop(projectId, packageId, evidence.canonical_observation_id)
      .then((blob) => {
        if (!blob.type.startsWith('image/')) throw new Error('The stored evidence is not an image.');
        objectUrl = URL.createObjectURL(blob);
        if (active) setCropUrl(objectUrl);
        else URL.revokeObjectURL(objectUrl);
      })
      .catch((cause) => {
        if (active) setError(cause instanceof Error ? cause.message : 'The evidence could not be loaded.');
      });

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
        </div>
        <span className="pdf-pane__page">
          <MapPin size={11} />
          Page {evidence.page}
        </span>
      </div>

      <div className="pdf-pane__crop">
        {!cropUrl && !error && <p className="pdf-pane__crop-state">Loading stored crop…</p>}
        {cropUrl && <img className="pdf-pane__crop-image" src={cropUrl} alt={`${label}, page ${evidence.page}: stored drawing crop`} />}
        {error && <p className="pdf-pane__crop-state pdf-pane__crop-state--error">{error}</p>}
      </div>

      <div className="pdf-pane__meta">
        <span className="pdf-pane__meta-label">Stored mechanical crop</span>
        <span className="pdf-pane__extractor">
          This is the recorded pixel region, not a full-drawing placement or redline.
        </span>
      </div>
    </div>
  );
}
