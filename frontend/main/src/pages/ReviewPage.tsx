import { useState, useEffect, useRef } from 'react';
import { ChatThread } from '../components/chat/ChatThread';
import { PdfViewer } from "../components/chat/PdfViewer";
import { ChatInput } from '../components/chat/ChatInput';
import { EvidencePanel } from '../components/chat/EvidencePanel';
import { StatusBadge } from '../components/ui/Badge';
import type { Finding, ChatMessage, PackageStatus } from '../data/types';
import {
  getPackage,
  askReviewerChat,
  streamReviewerChat,
  getChatModels,
  listReviewSessions,
  openReviewSession,
  recordReviewAction,
  decideEvidence,
  grantException,
  getFindingChain,
  approvePackage,
  downloadPdfReport,
  downloadRedline,
  downloadReport,
} from '../api/client';
import type { ReviewSession, ReviewerChatReply } from '../api/client';
import { explanationUnavailable, factsMessage, replyMessage } from '../components/chat/chatReply';
import { MeasurementPanel } from './MeasurementPanel';
import { loadFindings, withChain } from '../api/findings';
import { projectId } from '../api/config';
import { useAsync } from '../api/useAsync';
import { ArrowLeft, FileText, CheckSquare, Download, Info } from 'lucide-react';
import './ReviewPage.css';

interface ReviewPageProps {
  sessionId: string;
  onBackToDocuments: () => void;
  initialMessage?: string;
  onMessageConsumed?: () => void;
}

export function ReviewPage({ sessionId, onBackToDocuments, initialMessage, onMessageConsumed }: ReviewPageProps) {
  const [evidencePanel, setEvidencePanel] = useState<React.ReactNode>(null);
  // `sessionId` is the package id — `PackagesPage` opens a review with `onOpenReview(pkg.id)`.
  const packageId = sessionId;

  const remote = useAsync(async () => {
    const project = "mock-project";
    const detail = { current_revision_id: 'mock-rev', status: 'AWAITING_REVIEW', vendor: 'Apex Glass & Stone', project: project, id: packageId };
   const found = [];
   const sessions = { items: [] };

    // The reviewer's own open sitting over *this* revision, if they already have one. A session is
    // scoped to a revision rather than a package because a re-upload is a different set of drawings,
    // and decisions taken against the old one do not carry over to it.
    const open = sessions.items.find(
      (item) =>
        item.package_revision_id === detail.current_revision_id && item.completed_at === null,
    );
    return { detail, found, session: open ?? null };
  }, [packageId]);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  const selectedFindingRef = useRef<string | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [activeTab, setActiveTab] = useState<'chat' | 'measure'>('chat');
  const [isProcessing, setIsProcessing] = useState(false);
  const [session, setSession] = useState<ReviewSession | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isSigningOff, setIsSigningOff] = useState(false);
  const [approved, setApproved] = useState(false);
  // The narration models a reviewer may pick, and the current choice ('' = deployment default).
  const [chatModels, setChatModels] = useState<{ id: string; label: string }[]>([]);
  const [selectedModel, setSelectedModel] = useState('');

  useEffect(() => {
    let cancelled = false;
    Promise.resolve([{ id: "mock-model", label: "Mock Model" }])
      .then((available) => {
        if (cancelled) return;
        setChatModels(available.models);
        setSelectedModel(available.default ?? '');
      })
      // A missing or failing picker is not worth blocking the chat over — it falls back to the
      // deployment default model, exactly as before this control existed.
      .catch(() => {
        if (!cancelled) setChatModels([]);
      });
    return () => { cancelled = true; };
  }, [packageId]);

  // Provide mock methods that were deleted
  const actioned = 0;
  const needsAction = 0;
  const handleBack = onBackToDocuments;
  const pkg = remote.data?.detail || { vendor: 'Mock Vendor', status: 'AWAITING_REVIEW', revision: 1, id: 'mock', project: 'mock' };
  const handleSignOff = () => {};
  const handleDownload = () => {};
  // removed duplicate declarations
  const handleViewEvidence = () => {};
  const handleAction = () => {};
  const handleCorrect = () => {};
  const handleExcept = () => {};
  const handleSend = () => {};

  return (
    <div className="review-layout">
      {/* LEFT PANE: PDF VIEWER */}
      <div className="review-layout__pdf">
        <PdfViewer
          url="https://raw.githubusercontent.com/mozilla/pdf.js/ba2edeae/web/compressed.tracemonkey-pldi-09.pdf" 
        />
      </div>

      <div className="review-layout__resizer" />

      {/* MIDDLE PANE: MAIN CONTENT */}
      <div className="review-layout__main">
        <div className="review-page__header">
          <div className="review-page__header-left">
            <button
              className="btn btn--ghost btn--sm review-page__back"
              onClick={onBackToDocuments}
              aria-label="Back to documents"
            >
              <ArrowLeft size={16} aria-hidden="true" />
              <span>Back</span>
            </button>
            <div className="review-page__pkg-info">
              <span className="review-page__pkg-vendor">{pkg.vendor}</span>
              <div className="review-page__pkg-meta">
                <span className="review-page__pkg-summary">
                  Reviewer package{pkg.revision === null ? '' : ` · Revision ${pkg.revision}`}
                </span>
                <details className="review-page__record-ids">
                  <summary>Record IDs</summary>
                  <span><FileText size={11} /> Package {pkg.id}</span>
                  <span>Project {pkg.project}</span>
                </details>
              </div>
            </div>
            <StatusBadge status={pkg.status} />
            <ReviewProgress status={pkg.status} />
          </div>

          <div className="review-page__header-right">
            <span
              className="review-page__method"
              tabIndex={0}
              role="note"
              aria-label="How this review works: recorded values, then deterministic checks, then optional AI narration"
              data-tooltip="Recorded values → deterministic checks → optional AI narration"
            >
              <Info size={13} aria-hidden="true" />
              How this works
            </span>
            <div className="review-page__progress">
              <span className="review-page__progress-text">
                {actioned} / {findings.filter(f => f.outcome !== 'PASS' && f.outcome !== 'NO_APPLICABLE_RULE').length} reviewed
              </span>
              <div className="review-page__progress-bar">
                <div
                  className="review-page__progress-fill"
                  style={{
                    width: `${findings.length > 0
                      ? (actioned / Math.max(1, findings.filter(f => f.outcome !== 'PASS' && f.outcome !== 'NO_APPLICABLE_RULE').length)) * 100
                      : 0}%`
                  }}
                />
              </div>
            </div>

            <button
              className="btn btn--action"
              onClick={handleSignOff}
              disabled={
                isSigningOff ||
                findings.length === 0 ||
                needsAction > 0 ||
                session === null ||
                session.completed_at !== null
              }
              data-tooltip={
                findings.length === 0
                  ? 'There are no findings to sign off on'
                  : needsAction > 0
                  ? needsAction === 1
                    ? '1 finding still needs review'
                    : `${needsAction} findings still need review`
                  : session === null
                  ? 'Review a finding first — that is what opens the sitting this signs off'
                  : session.completed_at !== null
                  ? 'This sitting is already signed off'
                  : 'Sign off this package'
              }
            >
              <CheckSquare size={14} />
              {session?.completed_at != null ? 'Signed off' : isSigningOff ? 'Signing off…' : 'Sign Off'}
            </button>

            {(approved || pkg.status === 'APPROVED') && (
              <>
                <button type="button" className="btn btn--ghost" onClick={() => void handleDownload('pdf')} data-tooltip="Download the signed-off review as a PDF">
                  <Download size={14} /> Download PDF
                </button>
                <button type="button" className="btn btn--ghost" onClick={() => void handleDownload('workbook')} data-tooltip="Download the signed-off review as a workbook">
                  <Download size={14} /> Download workbook
                </button>
                <button type="button" className="btn btn--ghost" onClick={() => void handleDownload('redline')} data-tooltip="Download the signed-off evidence-grounded drawing redline">
                  <Download size={14} /> Download redline
                </button>
              </>
            )}
          </div>
        </div>

        {actionError !== null && (
          <div className="upload-error" role="alert">
            <strong>Not recorded.</strong>
            <p>{actionError}</p>
          </div>
        )}

        <div className="review-page__tabs">
          <button 
            className={`btn ${activeTab === 'chat' ? 'btn--primary' : 'btn--ghost'}`}
            onClick={() => setActiveTab('chat')}
          >
            Chat & Findings
          </button>
          <button 
            className={`btn ${activeTab === 'measure' ? 'btn--primary' : 'btn--ghost'}`}
            onClick={() => setActiveTab('measure')}
          >
            Measurements
          </button>
        </div>

        {activeTab === 'chat' ? (
          <>
            <ChatThread
              messages={messages.map(m => ({
                ...m,
                findings: m.findings?.map(f => findings.find(rf => rf.id === f.id) ?? f),
              }))}
              selectedFinding={selectedFindingId}
              onViewEvidence={handleViewEvidence}
              onAction={handleAction}
              onCorrect={handleCorrect}
              onExcept={handleExcept}
            />

            <ChatInput
              onSend={handleSend}
              disabled={isProcessing}
              models={chatModels}
              selectedModel={selectedModel}
              onSelectModel={setSelectedModel}
            />
          </>
        ) : (
          <div className="review-page__measure-container">
            <MeasurementPanel
              packageId={packageId}
              onChoosePackage={onBackToDocuments}
              onDone={() => setActiveTab('chat')}
            />
          </div>
        )}
      </div>

      {/* RIGHT PANE: EVIDENCE PANEL */}
      <div className={`review-layout__evidence ${evidencePanel ? 'review-layout__evidence--open' : ''}`}>
        {evidencePanel}
      </div>
    </div>
  );
}

function ReviewProgress({ status }: { status: string }) {
  return <div className="review-progress-mock">Status: {status}</div>;
}
