import { useState, useEffect } from 'react';
import { ChatThread } from '../components/chat/ChatThread';
import { ChatInput } from '../components/chat/ChatInput';
import { EvidencePanel } from '../components/chat/EvidencePanel';
import { StatusBadge } from '../components/ui/Badge';
import type { Finding, ChatMessage, PackageStatus } from '../data/types';
import {
  getPackage,
  listReviewSessions,
  openReviewSession,
  recordReviewAction,
  completeReviewSession,
} from '../api/client';
import type { ReviewSession } from '../api/client';
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
    const [detail, found, sessions] = await Promise.all([
      getPackage(project, packageId),
      loadFindings(project, packageId),
      listReviewSessions(project),
    ]);

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
  const [findings, setFindings] = useState<Finding[]>([]);
  const [isProcessing, setIsProcessing] = useState(false);
  const [session, setSession] = useState<ReviewSession | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isSigningOff, setIsSigningOff] = useState(false);
  const isLoading = remote.status === 'loading';

  // The fetched findings are the starting point; reviewer actions below are applied on top, so they
  // are not thrown away every time this re-renders.
  useEffect(() => {
    if (remote.status === 'ready') {
      setFindings(remote.data.found);
      setSession(remote.data.session);
    }
  }, [remote]);

  /**
   * The sitting these decisions belong to, opened on the first one rather than on arrival.
   *
   * Opening it when the page loads would mint a session every time somebody glanced at a package,
   * and a session is a record that a review happened. Looking is not reviewing.
   */
  async function ensureSession(): Promise<ReviewSession> {
    if (session !== null) return session;
    if (remote.status !== 'ready') throw new Error('The package is still loading.');

    const opened = await openReviewSession(
      projectId(),
      packageId,
      remote.data.detail.current_revision_id,
    );
    setSession(opened);
    return opened;
  }

  // Auto-send the question WelcomePage was carrying, once the findings it will be answered from
  // actually exist.
  //
  // **The fetched list is passed in rather than read from state.** Both effects run in the same
  // commit, so `findings` is still `[]` here — `setFindings` above has been scheduled, not applied.
  // The reply would have counted against an empty array and said "0 of 0 findings", which is not a
  // slow render, it is the screen stating something false about the package.
  useEffect(() => {
    if (remote.status === 'ready' && initialMessage) {
      handleSend(initialMessage, remote.data.found);
      onMessageConsumed?.();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [remote, initialMessage]);

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

  function handleSend(text: string, source: readonly Finding[] = findings) {
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
        ? source.filter(f => f.outcome === 'FAIL')
        : filter === 'REVIEW_REQUIRED'
        ? source.filter(f => f.outcome === 'REVIEW_REQUIRED')
        : filter === 'ALL'
        ? [...source]
        : undefined;

    const replyMsg: ChatMessage = {
      id: `msg-a-${Date.now()}`,
      role: 'assistant',
      content: describeFilter(filter, matched?.length ?? 0, source.length),
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

  /**
   * Record what the reviewer decided — on the server, which is the whole point of the ledger.
   *
   * This used to set local state and stop there, so every confirmation, correction, exception and
   * dismissal was discarded on refresh and nothing was ever written down. "A reviewer signs off" is
   * the fourth clause of the invariant, and it was the one clause with no persistence behind it.
   *
   * Shown immediately and rolled back if the write fails. A reviewer works down a list, and waiting
   * on a round trip per row makes that unusable — but a decision that silently did not save is worse
   * than a slow one, so a failure puts the row back and says so rather than leaving the tick.
   */
  async function handleAction(
    findingId: string,
    action: 'confirm' | 'correct' | 'except' | 'dismiss',
  ) {
    const previous = findings;
    setActionError(null);
    setFindings(prev => prev.map(f => (f.id === findingId ? { ...f, reviewer_action: action } : f)));

    try {
      const current = await ensureSession();
      await recordReviewAction(projectId(), current.id, { finding_id: findingId, action });
    } catch (error) {
      setFindings(previous);
      setActionError(
        `That decision was not recorded — ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  /** Close the sitting. Refused server-side if it is already complete, so this does not guess. */
  async function handleSignOff() {
    if (session === null || isSigningOff) return;
    setActionError(null);
    setIsSigningOff(true);
    try {
      const completed = await completeReviewSession(projectId(), session.id);
      setSession(completed);
    } catch (error) {
      setActionError(
        `Sign-off did not complete — ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      setIsSigningOff(false);
    }
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

          {/* Wired now. It had no handler at all, and its only guard was `needsAction > 0`, so on a
              package with no findings it rendered fully enabled — the one state in which signing off
              means attesting to a review that never ran. Both are conditions here. */}
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
                ? `${needsAction} finding${needsAction === 1 ? '' : 's'} still need review`
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
        </div>
      </div>

      {/* A write that failed, said out loud. The row has already been put back, so without this the
          reviewer would see their tick disappear and have no idea why — and might reasonably assume
          they had mis-clicked rather than that nothing was saved. */}
      {actionError !== null && (
        <div className="upload-error" role="alert">
          <strong>Not recorded.</strong>
          <p>{actionError}</p>
        </div>
      )}

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

/**
 * Read off the text, so the message and the list it produces can never disagree.
 *
 * **Order matters, and the obvious order was wrong.** "run full review" contains the word `review`,
 * so testing that first classified a request for *everything* as a request for the abstentions —
 * quietly dropping the failures, which are the findings somebody asking for a full review most needs
 * to see. The whole-set phrases are checked before the single-outcome words for that reason.
 */
function filterFor(text: string): FindingFilter {
  const t = text.toLowerCase();
  if (t.includes('full') || t.includes('everything') || t.includes('all')) return 'ALL';
  if (t.includes('fail')) return 'FAIL';
  if (t.includes('review')) return 'REVIEW_REQUIRED';
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
