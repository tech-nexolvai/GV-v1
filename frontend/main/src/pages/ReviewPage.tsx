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

    // Resolved from the findings already fetched for this package — no request, so no delay to
    // stage. The previous version waited 1400 ms to imitate thinking, which dressed a local array
    // filter up as a system doing work.
    const filter = filterFor(text);
    const matched =
      filter === 'FAIL'
        ? findings.filter(f => f.outcome === 'FAIL')
        : filter === 'REVIEW_REQUIRED'
        ? findings.filter(f => f.outcome === 'REVIEW_REQUIRED')
        : filter === 'ALL'
        ? findings
        : undefined;

    const replyMsg: ChatMessage = {
      id: `msg-a-${Date.now()}`,
      role: 'assistant',
      content: describeFilter(filter, matched?.length ?? 0, findings.length),
      timestamp: new Date().toISOString(),
      findings: matched,
    };
    setMessages(prev => prev.filter(m => !m.is_typing).concat(replyMsg));
    setIsProcessing(false);
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

/**
 * What the app actually did with a typed message.
 *
 * This replaced `getSimulatedReply`, which invented verdicts: it would answer "explain CT-1" with
 * *"shop 5980 mm vs arch 6012 mm, tolerance ±3.175 mm, verdict FAIL"* — numbers no drawing produced,
 * a rule nobody published, and a tolerance band that V1 does not have, since Raj settled on exact
 * match. Under **the AI reads, deterministic Python decides**, a screen that composes its own verdict
 * is the exact failure the architecture exists to prevent, and it is worse here than anywhere else
 * because this panel is where a reviewer signs off.
 *
 * There is no conversational endpoint yet. So this says only what it can defend: which filter it
 * applied, and how many of the package's real findings matched.
 */
function describeFilter(filter: FindingFilter, matched: number, total: number): string {
  const of = `${matched} of ${total} finding${total === 1 ? '' : 's'}`;

  switch (filter) {
    case 'FAIL':
      return `Showing the ${of} that failed. The verdicts come from the engine — nothing on this screen recomputes them.`;
    case 'REVIEW_REQUIRED':
      return `Showing the ${of} the engine could not decide, which are the ones needing your judgement.`;
    case 'ALL':
      return `Showing all ${total} finding${total === 1 ? '' : 's'} recorded for this package.`;
    case 'NONE':
      return (
        'Conversational review is not wired up yet — there is no endpoint behind this box, so nothing ' +
        'here can answer a question about a drawing. The findings below are the real ones for this ' +
        'package. Try "fail" or "review" to filter them.'
      );
  }
}

type FindingFilter = 'FAIL' | 'REVIEW_REQUIRED' | 'ALL' | 'NONE';

/** Read off the text, so the message and the list it produces can never disagree. */
function filterFor(text: string): FindingFilter {
  const t = text.toLowerCase();
  if (t.includes('fail')) return 'FAIL';
  if (t.includes('review')) return 'REVIEW_REQUIRED';
  if (t.includes('full') || t.includes('run') || t.includes('all')) return 'ALL';
  return 'NONE';
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
