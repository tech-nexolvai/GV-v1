import { useState, useEffect } from 'react';
import { ChatThread } from '../components/chat/ChatThread';
import { ChatInput } from '../components/chat/ChatInput';
import { EvidencePanel } from '../components/chat/EvidencePanel';
import { StatusBadge } from '../components/ui/Badge';
import type { Finding, ChatMessage, PackageStatus } from '../data/mock';
import { getPackage } from '../api/client';
import { loadFindings } from '../api/findings';
import { projectId } from '../api/config';
import { useAsync } from '../api/useAsync';
import { FileText, CheckSquare } from 'lucide-react';
import './ReviewPage.css';

interface ReviewPageProps {
  sessionId: string;
  onEvidenceChange: (panel: React.ReactNode) => void;
  initialMessage?: string;
  onMessageConsumed?: () => void;
}

export function ReviewPage({ sessionId, onEvidenceChange, initialMessage, onMessageConsumed }: ReviewPageProps) {
  // `sessionId` is the package id — `PackagesPage` opens a review with `onOpenReview(pkg.id)`.
  const packageId = sessionId;

  const remote = useAsync(async () => {
    const project = projectId();
    const [detail, found] = await Promise.all([
      getPackage(project, packageId),
      loadFindings(project, packageId),
    ]);
    return { detail, found };
  }, [packageId]);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const isLoading = remote.status === 'loading';

  // The fetched findings are the starting point; reviewer actions below are applied on top, so they
  // are not thrown away every time this re-renders.
  useEffect(() => {
    if (remote.status === 'ready') setFindings(remote.data.found);
  }, [remote]);

  // Auto-send the initial message from WelcomePage when loaded
  useEffect(() => {
    if (!isLoading && initialMessage) {
      handleSend(initialMessage);
      onMessageConsumed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [isLoading, initialMessage]);

  if (isLoading) {
    return <ReviewSkeleton />;
  }

  if (remote.status === 'error') {
    // Said plainly, and never as an empty thread. A review screen showing no findings is the
    // sentence "this drawing is clean" — the one thing a failed fetch must not be able to say.
    return (
      <div className="review-page review-page__error">
        <h2>This review could not be loaded</h2>
        <p>{remote.error.message}</p>
        <p>
          Nothing here has been checked. Do not read an empty list as a package with no findings.
        </p>
      </div>
    );
  }

  function handleSend(text: string) {
    if (isProcessing) return;

    // Add user message
    const userMsg: ChatMessage = {
      id: `msg-u-${Date.now()}`,
      role: 'user',
      content: text,
      timestamp: new Date().toISOString(),
    };

    // Add typing indicator
    const typingMsg: ChatMessage = {
      id: `msg-typing-${Date.now()}`,
      role: 'assistant',
      content: '',
      timestamp: new Date().toISOString(),
      is_typing: true,
    };

    setMessages(prev => [...prev, userMsg, typingMsg]);
    setIsProcessing(true);

    // Simulate a response after delay
    setTimeout(() => {
      const replyMsg: ChatMessage = {
        id: `msg-a-${Date.now()}`,
        role: 'assistant',
        content: getSimulatedReply(text),
        timestamp: new Date().toISOString(),
        findings: text.toLowerCase().includes('fail')
          ? findings.filter(f => f.outcome === 'FAIL')
          : text.toLowerCase().includes('review')
          ? findings.filter(f => f.outcome === 'REVIEW_REQUIRED')
          : text.toLowerCase().includes('full') || text.toLowerCase().includes('run')
          ? findings
          : undefined,
      };
      setMessages(prev => prev.filter(m => !m.is_typing).concat(replyMsg));
      setIsProcessing(false);
    }, 1400);
  }

  function handleViewEvidence(finding: Finding) {
    setSelectedFindingId(finding.id);
    onEvidenceChange(
      <EvidencePanel
        finding={finding}
        onClose={() => {
          setSelectedFindingId(null);
          onEvidenceChange(null);
        }}
      />
    );
  }

  function handleAction(findingId: string, action: 'confirm' | 'correct' | 'except' | 'dismiss') {
    setFindings(prev =>
      prev.map(f =>
        f.id === findingId
          ? { ...f, reviewer_action: action }
          : f
      )
    );
  }

  const pkg = {
    id: packageId,
    vendor: remote.status === 'ready' ? (remote.data.detail.vendor ?? '—') : '—',
    status: (remote.status === 'ready'
      ? remote.data.detail.state
      : 'CREATED') as PackageStatus,
    // The project id, until the API carries a human project name. An id a reviewer can quote beats
    // a friendly label that is not in any record.
    project: remote.status === 'ready' ? remote.data.detail.project_id : '',
  };
  const actioned = findings.filter(f => f.reviewer_action !== null).length;
  const needsAction = findings.filter(f =>
    f.reviewer_action === null &&
    f.outcome !== 'PASS' &&
    f.outcome !== 'NO_APPLICABLE_RULE'
  ).length;

  return (
    <div className="review-page">
      {/* Package header bar */}
      <div className="review-page__header">
        <div className="review-page__header-left">
          <div className="review-page__pkg-info">
            <span className="review-page__pkg-vendor">{pkg.vendor}</span>
            <div className="review-page__pkg-meta">
              <div className="review-page__pkg-id">
                <FileText size={11} />
                {pkg.id}
              </div>
              <span className="review-page__pkg-project">{pkg.project}</span>
            </div>
          </div>
          <StatusBadge status={pkg.status} />
        </div>

        <div className="review-page__header-right">
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
            disabled={needsAction > 0}
            data-tooltip={needsAction > 0 ? `${needsAction} findings still need review` : 'Sign off this package'}
          >
            <CheckSquare size={14} />
            Sign Off
          </button>
        </div>
      </div>

      {/* Messages */}
      <ChatThread
        messages={messages.map(m => ({
          ...m,
          findings: m.findings?.map(f => findings.find(rf => rf.id === f.id) ?? f),
        }))}
        selectedFinding={selectedFindingId}
        onViewEvidence={handleViewEvidence}
        onAction={handleAction}
      />

      {/* Input */}
      <ChatInput onSend={handleSend} disabled={isProcessing} />
    </div>
  );
}

function getSimulatedReply(text: string): string {
  const t = text.toLowerCase();
  if (t.includes('fail')) return 'Showing only **FAIL** findings. 1 critical failure detected — CT-1 width exceeds tolerance by 32 mm.';
  if (t.includes('review')) return 'Showing findings requiring your review. 2 items need attention before sign-off.';
  if (t.includes('explain') || t.includes('ct-1')) return '**CT-1 Width Verification** uses `within_tolerance`. The shop drawing shows **5980 mm** vs arch set **6012 mm**. Delta = 32 mm, tolerance = ±3.175 mm. Verdict: FAIL.\n\nThe extractor found this via `pdfplumber` (digital vector) on both documents — high confidence in the reading. The delta is nearly 10× the allowed tolerance.';
  if (t.includes('report') || t.includes('vendor')) return 'Vendor report cannot be generated until all FAIL and REVIEW findings have been reviewed. **2 items still need action.**';
  if (t.includes('full') || t.includes('run')) return 'Running full review against rulebook snapshot **CT-v1.2**. All 6 applicable checks executed for a 3-wall vanity configuration.';
  return 'Understood. Running check against the active rulebook snapshot. Results will appear below.';
}

function ReviewSkeleton() {
  return (
    <div className="review-page review-page--loading" style={{ opacity: 0.85 }}>
      {/* Header skeleton */}
      <div className="review-page__header" style={{ borderBottomColor: 'var(--border-subtle)' }}>
        <div className="review-page__header-left">
          <div className="skeleton" style={{ width: '80px', height: '18px' }} />
          <div className="skeleton" style={{ width: '120px', height: '14px', marginLeft: 'var(--space-3)' }} />
          <div className="skeleton" style={{ width: '60px', height: '18px', marginLeft: 'var(--space-3)' }} />
        </div>
        <div className="review-page__header-right">
          <div className="skeleton" style={{ width: '100px', height: '14px' }} />
          <div className="skeleton" style={{ width: '80px', height: '32px' }} />
        </div>
      </div>

      {/* Thread skeleton */}
      <div className="chat-thread" style={{ gap: 'var(--space-8)' }}>
        {/* User prompt skeleton */}
        <div className="chat-message chat-message--user">
          <div className="chat-message__avatar">
            <div className="skeleton" style={{ width: '28px', height: '28px', borderRadius: '50%' }} />
          </div>
          <div className="chat-message__content">
            <div className="skeleton" style={{ width: '140px', height: '24px', borderRadius: 'var(--radius-md) var(--radius-sm) var(--radius-md) var(--radius-md)' }} />
          </div>
        </div>

        {/* System response skeleton */}
        <div className="chat-message">
          <div className="chat-message__avatar">
            <div className="skeleton" style={{ width: '28px', height: '28px', borderRadius: 'var(--radius-md)' }} />
          </div>
          <div className="chat-message__content" style={{ gap: 'var(--space-4)' }}>
            <div className="skeleton" style={{ width: '420px', height: '16px' }} />
            <div className="skeleton" style={{ width: '280px', height: '16px' }} />
            
            {/* Finding cards skeletons */}
            <div className="chat-message__findings" style={{ marginTop: 'var(--space-3)' }}>
              <div className="skeleton" style={{ width: '80px', height: '12px', marginBottom: 'var(--space-2)' }} />
              <div style={{ display: 'flex', flexDirection: 'column', gap: 'var(--space-3)' }}>
                {[1, 2, 3].map(i => (
                  <div key={i} className="skeleton" style={{ width: '100%', height: '38px', borderRadius: 'var(--radius-md)' }} />
                ))}
              </div>
            </div>
          </div>
        </div>
      </div>

      {/* Input bar skeleton */}
      <div className="chat-input-area" style={{ borderTopColor: 'var(--border-subtle)' }}>
        <div className="skeleton" style={{ width: '100%', height: '48px', borderRadius: 'var(--radius-xl)' }} />
      </div>
    </div>
  );
}
