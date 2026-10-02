import { useState, useEffect, useRef } from 'react';
import { ChatThread } from '../components/chat/ChatThread';
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
import type { PackageDetail, ReviewSession, ReviewerChatReply } from '../api/client';
import { explanationUnavailable, factsMessage, replyMessage } from '../components/chat/chatReply';
import { MeasurementPanel } from './MeasurementPanel';
import { loadFindings, withChain } from '../api/findings';
import { projectId } from '../api/config';
import type { AsyncState } from '../api/useAsync';
import { checksFinished, isReviewWorking, reviewOverview, reviewActionCounts } from './reviewState';
import { ArrowLeft, RefreshCw } from 'lucide-react';
import { ReviewPackageDetails } from './ReviewPackageDetails';
import { ReviewHandoff } from './ReviewHandoff';
import { receiveReport } from './reviewHandoffState';
import type { DownloadState, ReportFormat } from './reviewHandoffState';
import './ReviewPage.css';

interface ReviewPageProps {
  sessionId: string;
  onEvidenceChange: (panel: React.ReactNode) => void;
  onBackToDocuments: () => void;
  /** Reports the vendor once the package has loaded, for the header title. */
  onTitleChange?: (title: string) => void;
  /** Refresh surrounding package lists after a confirmed server-side state change. */
  onPackageChanged?: () => void;
  evidenceDismissKey?: number;
  initialMessage?: string;
  onMessageConsumed?: () => void;
}

export function ReviewPage({ sessionId, onEvidenceChange, onBackToDocuments, onTitleChange, onPackageChanged, evidenceDismissKey, initialMessage, onMessageConsumed }: ReviewPageProps) {
  // `sessionId` is the package id — `PackagesPage` opens a review with `onOpenReview(pkg.id)`.
  const packageId = sessionId;

  const [remote, setRemote] = useState<AsyncState<{
    detail: PackageDetail;
    found: Finding[];
    session: ReviewSession | null;
    sessionError: string | null;
  }>>({ status: 'loading' });
  const [refreshKey, setRefreshKey] = useState(0);
  const [refreshError, setRefreshError] = useState<string | null>(null);
  const [waitingForChecks, setWaitingForChecks] = useState(false);
  const pendingChecks = useRef<{ previousIds: string[]; sawWorking: boolean } | null>(null);

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  const selectedFindingRef = useRef<string | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  const [activeTab, setActiveTab] = useState<'chat' | 'measure'>('chat');
  const [measurementOpened, setMeasurementOpened] = useState(false);
  const [isProcessing, setIsProcessing] = useState(false);
  const [session, setSession] = useState<ReviewSession | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isSigningOff, setIsSigningOff] = useState(false);
  const [approved, setApproved] = useState(false);
  const [download, setDownload] = useState<DownloadState>({ status: 'idle' });
  const downloadInFlight = useRef(false);
  // The narration models a reviewer may pick, and the current choice ('' = deployment default).
  const [chatModels, setChatModels] = useState<{ id: string; label: string }[]>([]);
  const [selectedModel, setSelectedModel] = useState('');

  useEffect(() => {
    selectedFindingRef.current = null;
    // The shell's Escape/outside-close also cancels an evidence request still in flight.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setSelectedFindingId(null);
    return () => {
      // A response from the previous package must not reopen evidence after navigation.
      selectedFindingRef.current = null;
    };
  }, [evidenceDismissKey]);

  useEffect(() => {
    let active = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    async function refresh() {
      try {
        const project = projectId();
        const [detail, found, sessionResult] = await Promise.all([
          getPackage(project, packageId),
          loadFindings(project, packageId),
          listReviewSessions(project).then(
            (data) => ({ data, error: null }),
            (error: unknown) => ({ data: null, error: error instanceof Error ? error.message : String(error) }),
          ),
        ]);
        if (!active) return;
        const open = sessionResult.data?.items.find((item) =>
          item.package_revision_id === detail.current_revision_id && item.completed_at === null,
        );
        setRemote({ status: 'ready', data: {
          detail, found, session: open ?? null, sessionError: sessionResult.error,
        } });
        setRefreshError(null);
        const pending = pendingChecks.current;
        if (pending) {
          pending.sawWorking ||= isReviewWorking(detail.state);
          if (checksFinished(detail.state, pending.previousIds, found, pending.sawWorking)) {
            pendingChecks.current = null;
            setWaitingForChecks(false);
          }
        }
        if (isReviewWorking(detail.state) || pendingChecks.current !== null) {
          timer = setTimeout(() => void refresh(), 2500);
        }
      } catch (error) {
        if (!active) return;
        const failure = error instanceof Error ? error : new Error(String(error));
        setRemote((current) => current.status === 'ready' ? current : { status: 'error', error: failure });
        setRefreshError(failure.message);
      }
    }
    void refresh();
    return () => { active = false; if (timer) clearTimeout(timer); };
  }, [packageId, refreshKey]);

  function openMeasurements() {
    setMeasurementOpened(true);
    setActiveTab('measure');
  }

  useEffect(() => {
    let cancelled = false;
    Promise.resolve().then(() => getChatModels(projectId(), packageId))
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
    return () => {
      cancelled = true;
    };
  }, [packageId]);
  const isLoading = remote.status === 'loading';

  // The header shows the vendor, which is what a reviewer calls a document set.
  const loadedVendor = remote.status === 'ready' ? remote.data.detail.vendor ?? 'Untitled document set' : null;
  useEffect(() => {
    if (loadedVendor !== null) onTitleChange?.(loadedVendor);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedVendor]);

  // The fetched findings are the starting point; reviewer actions below are applied on top, so they
  // are not thrown away every time this re-renders.
  useEffect(() => {
    if (remote.status === 'ready') {
      // This copies a freshly fetched package into locally editable review state.  Actions below
      // optimistically update it, so deriving it directly from `remote` would erase reviewer work.
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setFindings(remote.data.found);
      if (remote.data.sessionError === null) setSession(remote.data.session);
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
      void handleSend(initialMessage, remote.data.found);
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
          The recorded results are unavailable. Do not treat this as an empty or passing review.
        </p>
        <button className="btn btn--primary" onClick={() => setRefreshKey((key) => key + 1)}>Try again</button>
        <button className="btn btn--ghost" onClick={onBackToDocuments}>Back to documents</button>
      </div>
    );
  }

  async function handleSend(text: string, source: readonly Finding[] = findings) {
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

    const replyId = `msg-a-${Date.now()}`;
    const now = () => new Date().toISOString();
    let factsShown = false;
    try {
      let response: ReviewerChatReply;
      try {
        // Streamed: the findings table appears as soon as the server has selected it, while the
        // model is still writing. The explanation then replaces the pending line.
        response = await streamReviewerChat(
          projectId(),
          packageId,
          text,
          {
            onFacts: (facts) => {
              factsShown = true;
              const shown = factsMessage(facts, source, replyId, now());
              setMessages(prev => prev.filter(m => !m.is_typing).concat(shown));
            },
            onStage: (stage) => {
              setMessages(prev => prev.map(message => message.id === replyId || message.is_typing
                ? { ...message, streamStage: stage }
                : message));
            },
          },
          selectedModel || undefined,
        );
      } catch (streamError) {
        // After the facts are on screen they stay; only the explanation is reported missing.
        if (factsShown) throw streamError;
        // Before any facts arrived nothing has been shown, so ask once the plain way. This also
        // keeps chat working against a server that predates the stream.
        response = await askReviewerChat(projectId(), packageId, text, selectedModel || undefined);
      }
      const final = replyMessage(response, source, replyId, now());
      setMessages(prev =>
        factsShown
          ? prev.map(m => (m.id === replyId ? final : m))
          : prev.filter(m => !m.is_typing).concat(final),
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (factsShown) {
        // The findings on screen are the recorded run and still correct; keep them.
        setMessages(prev =>
          prev.map(m => (m.id === replyId ? explanationUnavailable(m, message) : m)),
        );
        return;
      }
      // A chat outage must not hide the already-fetched deterministic review. The page keeps its
      // ordinary finding cards and says clearly that it is showing that plain fallback.
      const replyMsg: ChatMessage = {
        id: `msg-a-${Date.now()}`,
        role: 'assistant',
        content: `The chat service could not return narration right now, so this uses deterministic findings only.\n${message}`,
        timestamp: new Date().toISOString(),
        findings: [...source],
        narration: {
          mode: 'structured_fallback',
          fallbackReason: message,
        },
      };
      setMessages(prev => prev.filter(m => !m.is_typing).concat(replyMsg));
    } finally {
      setIsProcessing(false);
    }
  }

  async function handleViewEvidence(finding: Finding) {
    selectedFindingRef.current = finding.id;
    setSelectedFindingId(finding.id);
    onEvidenceChange(
      <EvidencePanel
        finding={finding}
        projectId={projectId()}
        packageId={packageId}
        loading
        onClose={() => {
          selectedFindingRef.current = null;
          setSelectedFindingId(null);
          onEvidenceChange(null);
        }}
      />
    );
    try {
      const chain = await getFindingChain(projectId(), packageId, finding.id);
      const enriched = withChain(finding, chain);
      setFindings((current) => current.map((item) => (item.id === finding.id ? enriched : item)));
      // Chat cards keep the list snapshot that produced that reply.  Update that snapshot too, or
      // the evidence rail would have the chain while the card beside it continued to show the
      // sparse pre-fetch row — exactly the split view a reviewer cannot audit.
      setMessages((current) => current.map((message) => ({
        ...message,
        findings: message.findings?.map((item) => (item.id === finding.id ? enriched : item)),
      })));
      if (selectedFindingRef.current !== finding.id) return;
      onEvidenceChange(
        <EvidencePanel
          finding={enriched}
          projectId={projectId()}
          packageId={packageId}
          onClose={() => {
            selectedFindingRef.current = null;
            setSelectedFindingId(null);
            onEvidenceChange(null);
          }}
        />,
      );
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      if (selectedFindingRef.current !== finding.id) return;
      onEvidenceChange(
        <EvidencePanel
          finding={finding}
          projectId={projectId()}
          packageId={packageId}
          error={`Evidence could not be loaded — ${message}`}
          onClose={() => {
            selectedFindingRef.current = null;
            setSelectedFindingId(null);
            onEvidenceChange(null);
          }}
        />,
      );
    }
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

  /**
   * Correct a reading, and write the ledger.
   *
   * **This is what `correct` did not do.** The actions route takes a kind and a note, so a
   * correction recorded that something had been corrected without saying to what — and the
   * correction ledger, which `AGENTS.md` §2.6 makes the record of what we got wrong, stayed empty.
   * `D5.4` counts the reviewer correction rate off that table, so an empty one reads as "no
   * corrections were needed".
   *
   * The observation is the one the finding was decided from. A finding may rest on several, and
   * this corrects the first authoritative one — which is honest for a single-operand check and
   * wrong for a multi-operand one, so a finding with more than one is left to the evidence view
   * rather than guessed at here.
   */
  async function handleCorrect(findingId: string, correctedValue: string) {
    setActionError(null);
    try {
      const current = await ensureSession();
      const chain = await getFindingChain(projectId(), packageId, findingId);
      // Through `evidence`, which is where the chain puts the observation an operand came from —
      // and `null` there is meaningful: an operand a reviewer supplied has no observation behind it,
      // so there is nothing to correct rather than something to correct blindly.
      const observationIds = [...new Set(chain.operands.flatMap(operand =>
        operand.evidence ? [operand.evidence.canonical_observation_id] : [],
      ))];
      if (observationIds.length > 1) {
        setActionError('This finding uses several drawing readings. Inspect its evidence and correct the specific measurement in Measurements before running the checks again.');
        return;
      }
      const observationId = observationIds[0] ?? null;
      if (observationId === null) {
        setActionError(
          'This finding does not name a reading that can be corrected — it has no authoritative ' +
            'observation behind it, so there is nothing to correct.',
        );
        return;
      }
      await decideEvidence(projectId(), current.id, {
        finding_id: findingId,
        observation_id: observationId,
        action: 'correct',
        corrected_value: correctedValue,
      });
      setFindings(prev =>
        prev.map(f => (f.id === findingId ? { ...f, reviewer_action: 'correct' } : f)),
      );
    } catch (error) {
      setActionError(
        `That correction was not recorded — ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  /**
   * Accept one deviation, until a date.
   *
   * The scope is this finding and nothing wider. A reviewer saying "this one is acceptable" is not
   * the same as saying the rule should stop firing, and the second is a rule change that goes
   * through the rulebook where somebody reviews it.
   */
  async function handleExcept(findingId: string, reason: string, expiresAt: string) {
    setActionError(null);
    try {
      const current = await ensureSession();
      await grantException(projectId(), current.id, {
        finding_id: findingId,
        scope: 'finding',
        scope_id: findingId,
        reason,
        expires_at: expiresAt,
      });
      setFindings(prev =>
        prev.map(f => (f.id === findingId ? { ...f, reviewer_action: 'except' } : f)),
      );
    } catch (error) {
      setActionError(
        `That exception was not granted — ${error instanceof Error ? error.message : String(error)}`,
      );
    }
  }

  /**
   * Sign the package off.
   *
   * **This used to close the sitting and nothing else** — no approval, no state change, no check
   * that anything had been addressed — while the button said "Sign off this package". The approval
   * is what everything downstream depends on: `reports/publication.py:sign_off` refuses to release a
   * report without one, so a package could be "signed off" in the interface and unable to leave the
   * building.
   *
   * `approvePackage` closes the sitting too, as part of the same transaction. It refuses while any
   * REVIEW REQUIRED finding is unaddressed, which the button's own disabled state already reflects —
   * the server check is what makes that a rule rather than a hint.
   */
  async function handleSignOff() {
    if (isSigningOff) return;
    setActionError(null);
    setIsSigningOff(true);
    try {
      const current = await ensureSession();
      await approvePackage(projectId(), current.id);
      setApproved(true);
      // Re-fetch the authoritative package state for both the header and the sidebar.
      // Do not leave "Awaiting Review" beside successful sign-off/download controls.
      setRefreshKey((key) => key + 1);
      onPackageChanged?.();
      // Re-read rather than assume: approval completes the sitting server-side, and the package
      // state a moment ago is not the one the download button should be reading.
      // Approval already succeeded. An optional session refresh must not report that write as failed.
      setSession(await completeSessionState(current.id).catch(() => current));
    } catch (error) {
      setActionError(
        `Sign-off did not complete — ${error instanceof Error ? error.message : String(error)}`,
      );
    } finally {
      setIsSigningOff(false);
    }
  }

  /** The sitting as it now stands. Approval closed it; this reads back what was written. */
  async function completeSessionState(reviewSessionId: string) {
    const sessions = await listReviewSessions(projectId());
    const found = sessions.items.find((item) => item.id === reviewSessionId);
    return found ?? session;
  }

  /**
   * Hand the reviewer either signed-off handoff artifact.
   *
   * The blob is turned into a click here rather than linking straight at the endpoint, so a refusal
   * — not approved, no report generated — surfaces as a message instead of a download that silently
   * does nothing.
   */
  async function handleDownload(format: ReportFormat) {
    if (downloadInFlight.current || !(approved || (remote.status === 'ready' && remote.data.detail.state === 'APPROVED')) ||
      waitingForChecks || (remote.status === 'ready' && isReviewWorking(remote.data.detail.state))) return;
    downloadInFlight.current = true;
    setDownload({ status: 'loading', format });
    try {
      await receiveReport(format, (selected) => selected === 'pdf'
        ? downloadPdfReport(projectId(), packageId)
        : selected === 'redline'
          ? downloadRedline(projectId(), packageId)
          : downloadReport(projectId(), packageId), (blob) => {
        const url = URL.createObjectURL(blob);
        const link = document.createElement('a');
        link.href = url;
        link.download = `gv-review-${packageId}${format === 'redline' ? '-redline' : ''}.${format === 'workbook' ? 'xlsx' : 'pdf'}`;
        document.body.append(link);
        try { link.click(); } finally {
          link.remove();
          // Let the browser consume the object URL before releasing it.
          window.setTimeout(() => URL.revokeObjectURL(url), 1000);
        }
      });
      setDownload({ status: 'started', format });
    } catch (error) {
      setDownload({ status: 'error', format, message: error instanceof Error ? error.message : String(error) });
    } finally {
      downloadInFlight.current = false;
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
    revision: remote.status === 'ready' ? remote.data.detail.current_revision_number : null,
  };
  const { reviewed: actioned, total: requiringReview } = reviewActionCounts(findings);
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
          <button
            type="button"
            className="btn btn--ghost btn--sm review-page__back"
            onClick={onBackToDocuments}
            aria-label="Back to documents"
            title="Back to documents"
          >
            <ArrowLeft size={13} />
            <span>Documents</span>
          </button>
          <div className="review-page__pkg-info">
            <span className="review-page__pkg-vendor">{pkg.vendor}</span>
            <div className="review-page__pkg-meta">
              <span className="review-page__pkg-summary">
                Reviewer package{pkg.revision === null ? '' : ` · Revision ${pkg.revision}`}
              </span>
            </div>
          </div>
          <StatusBadge status={pkg.status} />
          <ReviewPackageDetails packageId={pkg.id} projectId={pkg.project}>
            <ReviewProgress status={pkg.status} />
          </ReviewPackageDetails>
        </div>

        <div className="review-page__header-right">
          <ReviewHandoff
            findingsCount={findings.length}
            reviewed={actioned}
            requiringReview={requiringReview}
            needsAction={needsAction}
            approved={approved || pkg.status === 'APPROVED'}
            sessionCompleted={session?.completed_at != null}
            signing={isSigningOff}
            working={waitingForChecks || isReviewWorking(pkg.status)}
            download={download}
            onSignOff={() => void handleSignOff()}
            onDownload={(format) => void handleDownload(format)}
          />
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

      {remote.status === 'ready' && remote.data.sessionError && (
        <div className="review-page__notice" role="status">
          Your saved review session could not be loaded. The recorded findings remain available.
          <button className="btn btn--ghost btn--sm" onClick={() => setRefreshKey((key) => key + 1)}>Retry</button>
        </div>
      )}
      {refreshError && (
        <div className="review-page__notice" role="alert">
          Updates could not be loaded: {refreshError}. Showing the last recorded results.
          <button className="btn btn--ghost btn--sm" onClick={() => setRefreshKey((key) => key + 1)}>Retry</button>
        </div>
      )}
      {waitingForChecks && (
        <div className="review-page__notice" role="status">
          Checks requested. Watching for updated results; any findings below are from the last recorded run.
        </div>
      )}

      {/* View Tabs */}
      <div className="review-page__tabs">
        <button 
          className={`btn ${activeTab === 'chat' ? 'btn--primary' : 'btn--ghost'}`}
          onClick={() => setActiveTab('chat')}
        >
          Chat & Findings
        </button>
        <button 
          className={`btn ${activeTab === 'measure' ? 'btn--primary' : 'btn--ghost'}`}
          onClick={openMeasurements}
        >
          Measurements
        </button>
        <button className="btn btn--ghost review-page__refresh" aria-label="Refresh results"
          title="Refresh results" onClick={() => setRefreshKey((key) => key + 1)}>
          <RefreshCw size={16} aria-hidden="true" /><span>Refresh results</span>
        </button>
      </div>

      {activeTab === 'chat' ? (
        <>
          {findings.length === 0 && (
            <div className="review-page__notice">
              <button className="btn btn--primary" onClick={openMeasurements}>Open measurements</button>
            </div>
          )}
          {/* Messages */}
          <ChatThread
            messages={[reviewOverview(packageId, pkg.status, findings,
              remote.status === 'ready' ? remote.data.detail.created_at : ''), ...messages].map(m => ({
              ...m,
              findings: m.findings?.map(f => findings.find(rf => rf.id === f.id) ?? f),
            }))}
            selectedFinding={selectedFindingId}
            onViewEvidence={handleViewEvidence}
            onAction={handleAction}
            onCorrect={handleCorrect}
            onExcept={handleExcept}
          />

          {/* Input */}
          <ChatInput
            onSend={handleSend}
            prompts={findings.length === 0 ? [] : undefined}
            disabled={isProcessing}
            models={chatModels}
            selectedModel={selectedModel}
            onSelectModel={setSelectedModel}
          />
        </>
      ) : null}
      {measurementOpened && (
        <div className="review-page__measure-container" hidden={activeTab !== 'measure'}>
          <MeasurementPanel
            packageId={packageId}
            onChoosePackage={onBackToDocuments}
            onDone={() => setActiveTab('chat')}
            onChecksRequested={() => {
              selectedFindingRef.current = null;
              setSelectedFindingId(null);
              onEvidenceChange(null);
              pendingChecks.current = { previousIds: findings.map((finding) => finding.id), sawWorking: false };
              setWaitingForChecks(true);
              setApproved(false);
              setActiveTab('chat');
              setRefreshKey((key) => key + 1);
            }}
          />
        </div>
      )}
    </div>
  );
}

const REVIEW_STEPS = ['Upload', 'Confirm / type', 'Run checks', 'Review', 'Sign off', 'Download'] as const;

function ReviewProgress({ status }: { status: PackageStatus }) {
  const completed = status === 'APPROVED' ? 5 : status === 'AWAITING_REVIEW' ? 3 : 0;
  const failed = status === 'FAILED_PERMANENT' || status === 'FAILED_RETRYABLE';
  return (
    <div className="review-path" aria-label="Human-operated review progress">
      <span className="review-path__label">{failed ? 'Workflow needs attention' : 'Human-operated path'}</span>
      <ol className="review-path__steps">
        {REVIEW_STEPS.map((step, index) => (
          <li key={step} className={index < completed ? 'review-path__step review-path__step--done' : index === completed && !failed ? 'review-path__step review-path__step--current' : 'review-path__step'}>
            {step}
          </li>
        ))}
      </ol>
    </div>
  );
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
