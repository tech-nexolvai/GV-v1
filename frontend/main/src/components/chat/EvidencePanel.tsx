import { useEffect, useState } from 'react';
import { ArrowLeft, X, FileImage, MapPin, ExternalLink, RefreshCw } from 'lucide-react';
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
  const locations = finding.evidence ?? [
    ...(finding.arch_evidence ? [{ ...finding.arch_evidence, document_role: 'ARCH' }] : []),
    ...(finding.shop_evidence ? [{ ...finding.shop_evidence, document_role: 'SHOP' }] : []),
  ];
  const hasMappedCrop = locations.length > 0;
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

    if (finding.recorded_operands === undefined) {
      return 'The recorded details have not loaded yet. Return to the finding and retry Evidence & facts.';
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
          {finding.name !== finding.check_id && <span className="evidence-panel__name">{finding.name}</span>}
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

        {!loading && !error && locations.map((evidence, index) => (
          <CropPane
            key={`${projectId}:${packageId}:${evidence.canonical_observation_id}:${index}`}
            evidence={evidence}
            operands={recordedOperands.filter((operand) => operand.canonicalObservationId === evidence.canonical_observation_id)}
            projectId={projectId}
            packageId={packageId}
          />
        ))}

        {/* No crop is intentionally not rendered as a plausible stand-in. A reviewer must be able to
            distinguish a rule result from visual drawing evidence. */}
        {!loading && !error && !hasMappedCrop && (
          <div className="evidence-panel__no-evidence">
            <FileImage size={24} className="evidence-panel__no-evidence-icon" />
            <p>{finding.recorded_operands === undefined
              ? 'Evidence details have not loaded.'
              : 'No drawing crop is available for this check.'}</p>
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
                A drawing crop appears when the recorded drawing reading has stored visual evidence.
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

// Crops are verified images. Full drawing PDFs remain a separate viewer and artifact.
interface CropPaneProps {
  evidence: NonNullable<Finding['arch_evidence']>;
  operands: NonNullable<Finding['recorded_operands']>;
  projectId: string;
  packageId: string;
}

function CropPane({ evidence, operands, projectId, packageId }: CropPaneProps) {
  const role = evidence.document_role ?? 'RECORDED';
  const label = role.toUpperCase() === 'ARCH' ? 'Architectural set'
    : role.toUpperCase() === 'SHOP' ? 'Vendor shop drawing' : `Recorded drawing (${role})`;
  const [cropUrl, setCropUrl] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let active = true;
    let objectUrl: string | null = null;
    downloadEvidenceCrop(projectId, packageId, evidence.canonical_observation_id)
      .then((blob) => {
        if (!active) return;
        if (!blob.type.startsWith('image/') || blob.size === 0) {
          throw new Error('The evidence service did not return a drawing image. Please retry.');
        }
        objectUrl = URL.createObjectURL(blob);
        setCropUrl(objectUrl);
      })
      .catch((cause) => {
        if (active) setError(cause instanceof Error ? cause.message : 'The stored crop could not be loaded.');
      });

    return () => {
      active = false;
      if (objectUrl !== null) URL.revokeObjectURL(objectUrl);
    };
  }, [evidence.canonical_observation_id, packageId, projectId, attempt]);

  function retry() {
    setCropUrl(null);
    setError(null);
    setAttempt((current) => current + 1);
  }

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

      <div className="pdf-pane__crop" aria-busy={!cropUrl && !error}>
        {!cropUrl && !error && <p className="pdf-pane__crop-state" role="status">Loading recorded drawing crop…</p>}
        {cropUrl && !error && <img
          className="pdf-pane__crop-image"
          src={cropUrl}
          alt={`${label}, page ${evidence.page}: stored crop for ${evidence.semantic_type.replaceAll('_', ' ')}`}
          onError={() => setError('The stored image could not be displayed. Please retry.')}
        />}
        {error && <div className="pdf-pane__crop-state pdf-pane__crop-state--error" role="alert">
          <p>{error}</p>
          <button className="btn btn--secondary btn--sm" onClick={retry}><RefreshCw size={14} /> Retry crop</button>
        </div>}
      </div>

      <div className="pdf-pane__meta">
        <span className="pdf-pane__meta-label">Stored drawing crop</span>
        {cropUrl && !error && <a className="btn btn--ghost btn--sm" href={cropUrl} target="_blank" rel="noreferrer">
          <ExternalLink size={13} /> Open full-size crop
        </a>}
      </div>
      <p className="pdf-pane__explanation">The recorded source region for this reading. It is not a redline.</p>
      {operands.length > 0 && <dl className="pdf-pane__values">
        {operands.map((operand, index) => <div key={`${operand.name}:${index}`}>
          <dt>{operand.name.replaceAll('_', ' ')}</dt>
          <dd><strong>{operand.value}</strong><span>{operand.source} · {operand.status}</span></dd>
        </div>)}
      </dl>}
      <details className="pdf-pane__provenance">
        <summary>Recorded evidence details</summary>
        <dl>
          <dt>Semantic type</dt><dd>{evidence.semantic_type}</dd>
          <dt>Observation</dt><dd>{evidence.canonical_observation_id}</dd>
          <dt>Document version</dt><dd>{evidence.document_version_id}</dd>
          {evidence.recorded_location && <>
            <dt>Authority</dt><dd>{evidence.recorded_location.authority}</dd>
            <dt>Coordinate space</dt><dd>{evidence.recorded_location.coordinate_space}</dd>
            <dt>Page ID</dt><dd>{evidence.recorded_location.page_id}</dd>
          </>}
          <dt>Recorded polygon</dt><dd>{JSON.stringify(evidence.recorded_location?.polygon ?? evidence.polygon)}</dd>
        </dl>
      </details>
    </div>
  );
}
