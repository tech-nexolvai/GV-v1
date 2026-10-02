import { useEffect, useId, useRef } from 'react';
import { CheckSquare, ChevronDown, Download, FileSpreadsheet, FileText, ScanLine } from 'lucide-react';
import { REPORT_LABELS, handoffAvailability } from './reviewHandoffState.js';
import type { DownloadState, ReportFormat } from './reviewHandoffState.js';

interface ReviewHandoffProps {
  findingsCount: number;
  reviewed: number;
  requiringReview: number;
  needsAction: number;
  approved: boolean;
  sessionCompleted: boolean;
  signing: boolean;
  working: boolean;
  download: DownloadState;
  onSignOff: () => void;
  onDownload: (format: ReportFormat) => void;
}

const REPORTS = [
  { format: 'pdf', description: 'Branded review summary and findings', Icon: FileText },
  { format: 'workbook', description: 'Recorded findings in an Excel workbook', Icon: FileSpreadsheet },
  { format: 'redline', description: 'Located findings marked on the drawing', Icon: ScanLine },
] as const;

export function ReviewHandoff(props: ReviewHandoffProps) {
  const hintId = useId();
  const disclosure = useRef<HTMLDetailsElement>(null);
  const { signOffDisabled, downloadsDisabled, message } = handoffAvailability(props);
  const downloading = props.download.status === 'loading';

  useEffect(() => {
    function close(event: PointerEvent | KeyboardEvent) {
      const element = disclosure.current;
      if (!element?.open) return;
      if (event instanceof KeyboardEvent) {
        if (event.key !== 'Escape') return;
        element.open = false;
        element.querySelector('summary')?.focus();
      } else if (event.target instanceof Node && !element.contains(event.target)) {
        element.open = false;
      }
    }
    document.addEventListener('pointerdown', close);
    document.addEventListener('keydown', close);
    return () => {
      document.removeEventListener('pointerdown', close);
      document.removeEventListener('keydown', close);
    };
  }, []);

  return <section className="review-handoff" aria-label="Review sign-off and reports">
    <div className="review-handoff__actions">
      <div className="review-page__progress">
        <span className="review-page__progress-text">{props.reviewed} / {props.requiringReview} reviewed</span>
        <div className="review-page__progress-bar" aria-hidden="true">
          <div className="review-page__progress-fill" style={{ width: `${props.reviewed / Math.max(1, props.requiringReview) * 100}%` }} />
        </div>
      </div>
      {!props.approved && <button type="button" className="btn btn--action"
        disabled={signOffDisabled} onClick={props.onSignOff} aria-describedby={hintId}>
        <CheckSquare size={14} aria-hidden="true" />
        {props.signing ? 'Signing off…' : 'Sign Off'}
      </button>}
      {props.approved && <details className="review-handoff__downloads" ref={disclosure}>
        <summary className="btn btn--action" aria-describedby={hintId}>
          <Download size={14} aria-hidden="true" /> Reports <ChevronDown size={14} aria-hidden="true" />
        </summary>
        <div className="review-handoff__options" aria-label="Download a report">
          <p className="review-handoff__options-title">Download a report</p>
          {REPORTS.map(({ format, description, Icon }) => <button key={format} type="button"
            className="review-handoff__option" disabled={downloadsDisabled || downloading}
            onClick={() => props.onDownload(format)}>
            <Icon size={18} aria-hidden="true" />
            <span><strong>{REPORT_LABELS[format]}</strong><small>{description}</small></span>
          </button>)}
          <p className="review-handoff__availability">The server checks report availability on request. A redline requires located drawing evidence.</p>
        </div>
      </details>}
    </div>
    <p className="review-handoff__hint" id={hintId}>{message}</p>
    <div className="review-handoff__download-state" aria-live="polite" aria-atomic="true">
      {props.download.status === 'loading' && <p role="status">Requesting {REPORT_LABELS[props.download.format].toLowerCase()}…</p>}
      {props.download.status === 'started' && <p role="status">{REPORT_LABELS[props.download.format]} received. Download started; check your browser’s downloads.</p>}
    </div>
    {props.download.status === 'error' && <p className="review-handoff__error" role="alert">
      {REPORT_LABELS[props.download.format]} could not be downloaded. {props.download.message}
    </p>}
  </section>;
}
