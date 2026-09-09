import { useEffect, useState } from 'react';
import { X, FileImage, MapPin } from 'lucide-react';
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

        {/* No evidence */}
        {!loading && !error && !finding.arch_evidence && !finding.shop_evidence && (
          <div className="evidence-panel__no-evidence">
            <FileImage size={24} className="evidence-panel__no-evidence-icon" />
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
