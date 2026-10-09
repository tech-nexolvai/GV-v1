import { lazy, Suspense, useState, useEffect, useRef } from 'react';
import { ChatThread } from '../components/chat/ChatThread';
import type { DecisionSaveResult, SimpleReviewAction } from '../components/chat/decisionSave';
import { ChatInput } from '../components/chat/ChatInput';
import { EvidencePanel } from '../components/chat/EvidencePanel';
import { targetFromFinding, targetFromRow, type ViewerTarget } from '@/lib/drawing-viewer';
import { ResultsDashboard, type CountertopsState } from '@/components/results/results-dashboard';
import type { BulkResult } from '@/components/results/other-checks';
import { recordEach, signOffSummary, type Filter } from '@/lib/countertop-results';
import { architectFindingIds } from '@/lib/architect';
import { Sheet, SheetContent, SheetDescription, SheetHeader, SheetTitle } from '@/components/ui/sheet';
import { Tabs, TabsList, TabsTrigger } from '@/components/ui/tabs';
import { Button } from '@/components/ui/button';
import { MessageSquare } from 'lucide-react';
import { canSignOff, decisionPayload } from '../components/output/reviewerResults';
import { receiveReport, type DownloadState, type ReportFormat } from '../components/output/reportDownload';
import type { Finding, ChatMessage, PackageStatus } from '../data/types';
import {
  getPackage,
  getApprovalReadiness,
  getCountertopResults,
  getSignedExports,
  prepareSignedExports,
  getChangedValues,
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
import type { ReviewSession, ReviewerChatReply, ApprovalReadiness, CountertopResult } from '../api/client';
import { explanationUnavailable, factsMessage, replyMessage, withStreamStage } from '../components/chat/chatReply';
import { MeasurementPanel } from './MeasurementPanel';
import { loadFindings, withChain } from '../api/findings';
import { projectId } from '../api/config';
import { useAsync } from '../api/useAsync';
import { HeaderActions, HeaderTitleExtra } from '../components/shell/ShellHeader';
import { reviewStage, type NextActionKind } from '@/lib/review-stage';
import { ReviewStepper } from '@/components/review/review-stepper';
import { NextActionButton, type SecondaryAction } from '@/components/review/next-action';
import { ChangedValuesBadge } from '@/components/review/changed-values-badge';
import { RecordIdsDialog } from '@/components/review/record-ids-dialog';
import { SignOffDialog, SignOffPanel, type SignOffScope } from '@/components/review/signoff-panel';
import { ReportPanel } from '@/components/review/report-panel';
import { ChangedValuesPanel } from '@/components/output/ChangedValuesPanel';
import { Dialog, DialogContent, DialogTitle } from '@/components/ui/dialog';
import { PackageStatusBadge } from '@/components/ui/package-status-badge';
import { Skeleton } from '@/components/ui/skeleton';
import { recordReviewDecision } from './recordReviewDecision';
import './ReviewPage.css';

// The drawing viewer (#1045) is fetched the first time a reviewer opens a drawing.
const DrawingViewerSheet = lazy(() => import('@/components/drawing/drawing-viewer').then((m) => ({ default: m.DrawingViewerSheet })));
// The "Needs you" queue (#1050), fetched the first time it is opened.
const NeedsYouQueue = lazy(() => import('@/components/queue/needs-you-queue').then((m) => ({ default: m.NeedsYouQueue })));

/** States in which the server is reading or checking on its own; the stepper follows them (#1034). */
const PROCESSING_STATES = new Set([
  'UPLOADING', 'UPLOADED', 'INGESTING', 'EXTRACTING', 'MATCHING', 'VALIDATING_EVIDENCE',
  'RUNNING_CHECKS', 'GENERATING_OUTPUTS', 'FAILED_RETRYABLE',
]);

interface ReviewPageProps {
  sessionId: string;
  onEvidenceChange: (panel: React.ReactNode) => void;
  onBackToDocuments: () => void;
  /** Reports the vendor and revision once the package has loaded, for the header breadcrumb. */
  onTitleChange?: (title: string, revision: number | null) => void;
  /** Reports how many findings still need the reviewer, so the sidebar shows the live count. */
  onNeedYouChange?: (count: number | null) => void;
  onPackageChanged?: () => void;
  initialMessage?: string;
  onMessageConsumed?: () => void;
}

export function ReviewPage({ sessionId, onEvidenceChange, onBackToDocuments, onTitleChange, onNeedYouChange, onPackageChanged, initialMessage, onMessageConsumed }: ReviewPageProps) {
  // `sessionId` is the package id — `PackagesPage` opens a review with `onOpenReview(pkg.id)`.
  const packageId = sessionId;

  const remote = useAsync(async () => {
    const project = projectId();
    const detail = await getPackage(project, packageId);
    const [found, sessions, readiness] = await Promise.all([
      loadFindings(project, packageId, detail.current_revision_id),
      listReviewSessions(project),
      getApprovalReadiness(project, packageId),
    ]);

    // The reviewer's own open sitting over *this* revision, if they already have one. A session is
    // scoped to a revision rather than a package because a re-upload is a different set of drawings,
    // and decisions taken against the old one do not carry over to it.
    const open = sessions.items.find(
      (item) =>
        item.package_revision_id === detail.current_revision_id && item.completed_at === null,
    );
    // Who is signed in, as the server's own record of their sittings says (the list is theirs only,
    // `mine` defaults to true): the name the sign-off confirmation shows (#1064).
    const me = sessions.items[0]?.reviewer ?? null;
    return { detail, found, session: open ?? null, readiness, me };
  }, [packageId]);
  const [resultsVersion, setResultsVersion] = useState(0);
  const changedValues = useAsync(
    () => getChangedValues(projectId(), packageId),
    [packageId, resultsVersion],
  );

  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  const selectedFindingRef = useRef<string | null>(null);
  const [findings, setFindings] = useState<Finding[]>([]);
  // The review opens on Results (#1039); chat lives in a side sheet.
  const [activeTab, setActiveTab] = useState<'results' | 'measure'>('results');
  const [chatOpen, setChatOpen] = useState(Boolean(initialMessage));
  // "Show on drawing" (#1045): what the drawing viewer points at; `opening` restarts it fitted each time.
  const [viewer, setViewer] = useState<{ opening: number; target: ViewerTarget } | null>(null);
  const [openings, setOpenings] = useState(0);
  // The "Needs you" queue (#1050): `queueOpening` restarts it with a fresh item list each time.
  const [queueOpen, setQueueOpen] = useState(false);
  const [queueOpening, setQueueOpening] = useState(0);
  // The countertop results (#1035), loaded beside the findings; earlier rows stay on screen while a
  // refresh is in flight, so the table never flashes back to a skeleton.
  const [countertops, setCountertops] = useState<CountertopsState>({ status: 'loading' });
  const [countertopsVersion, setCountertopsVersion] = useState(0);
  const [measureVisited, setMeasureVisited] = useState(false);
  const [targetRow, setTargetRow] = useState<string | null>(null);
  const [readiness, setReadiness] = useState<ApprovalReadiness | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const refreshPending = useRef(false);
  const [waitingForChecks, setWaitingForChecks] = useState(false);
  const checksBaseline = useRef('');
  const [isProcessing, setIsProcessing] = useState(false);
  const [session, setSession] = useState<ReviewSession | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [isSigningOff, setIsSigningOff] = useState(false);
  // The sign-off confirmation (#1064): every Sign off button opens it; only its confirm signs.
  const [signOffOpen, setSignOffOpen] = useState(false);
  const [signOffError, setSignOffError] = useState<string | null>(null);
  const [approved, setApproved] = useState(false);
  const [recordedStatus, setRecordedStatus] = useState<PackageStatus | null>(null);
  const [downloadState, setDownloadState] = useState<DownloadState>({ status: 'idle' });
  const downloadPending = useRef(false);
  // The narration models a reviewer may pick, and the current choice ('' = deployment default).
  const [chatModels, setChatModels] = useState<{ id: string; label: string }[]>([]);
  const [selectedModel, setSelectedModel] = useState('');
  // #1034: values saved in this session after the last check run (the API records no time for values).
  const [valuesChangedSinceRun, setValuesChangedSinceRun] = useState(false);
  // The Results filter, held here so "Review N items" can open it on what needs the reviewer.
  const [resultsFilter, setResultsFilter] = useState<Filter | null>(null);
  const [recordIdsOpen, setRecordIdsOpen] = useState(false);
  const [projectValuesOpen, setProjectValuesOpen] = useState(false);
  // Signed-export status after approval, for the Report step and the Download action.
  const [exportsStatus, setExportsStatus] = useState<'not_requested' | 'preparing' | 'ready' | 'failed' | null>(null);
  const [exportsError, setExportsError] = useState<string | null>(null);
  const [exportsVersion, setExportsVersion] = useState(0);
  const [preparingExports, setPreparingExports] = useState(false);
  // A failed "Prepare signed files" request, kept apart from a failed status check (#1064).
  const [prepareError, setPrepareError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    getChatModels(projectId(), packageId)
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
  const loadedRevision = remote.status === 'ready' ? remote.data.detail.current_revision_number : null;
  useEffect(() => {
    if (loadedVendor !== null) onTitleChange?.(loadedVendor, loadedRevision);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [loadedVendor, loadedRevision]);

  // The sidebar shows this review's live "need you" count, not the one it loaded earlier.
  const blockingCount = readiness?.blocking_findings ?? null;
  useEffect(() => {
    if (blockingCount !== null) onNeedYouChange?.(blockingCount);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [blockingCount]);

  // The fetched findings are the starting point; reviewer actions below are applied on top, so they
  // are not thrown away every time this re-renders.
  useEffect(() => {
    if (remote.status === 'ready') {
      // This copies a freshly fetched package into locally editable review state.  Actions below
      // optimistically update it, so deriving it directly from `remote` would erase reviewer work.
      setFindings(remote.data.found);
      setSession(remote.data.session);
      setReadiness(remote.data.readiness);
    }
  }, [remote]);

  useEffect(() => {
    if (!waitingForChecks) return;
    const timer = window.setInterval(() => void refreshResults(), 2000);
    return () => window.clearInterval(timer);
    // The interval refreshes saved data only; it never remounts Measurements or its drafts.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [waitingForChecks, packageId]);

  // While the server is reading or checking, re-read the saved state every few seconds so the
  // stepper moves on by itself (#1034). Read-only: the same refresh the Refresh button runs.
  const liveState = recordedStatus ?? (remote.status === 'ready' ? remote.data.detail.state : null);
  const serverBusy = liveState !== null && PROCESSING_STATES.has(liveState);
  useEffect(() => {
    if (!serverBusy || waitingForChecks) return;
    const timer = window.setInterval(() => void refreshResults(), 5000);
    return () => window.clearInterval(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [serverBusy, waitingForChecks, packageId]);

  // After approval: whether the signed files are ready, re-asked while they are being prepared.
  const isApproved = approved || liveState === 'APPROVED';
  useEffect(() => {
    if (!isApproved) return;
    let current = true;
    getSignedExports(projectId(), packageId).then(
      (answer) => { if (current) { setExportsStatus(answer.status); setExportsError(null); } },
      (error: unknown) => { if (current) { setExportsStatus(null); setExportsError(error instanceof Error ? error.message : String(error)); } },
    );
    return () => { current = false; };
  }, [isApproved, packageId, exportsVersion]);
  useEffect(() => {
    if (exportsStatus !== 'preparing') return;
    const timer = window.setTimeout(() => setExportsVersion((n) => n + 1), 4000);
    return () => window.clearTimeout(timer);
  }, [exportsStatus, exportsVersion]);

  useEffect(() => {
    let current = true;
    getCountertopResults(projectId(), packageId).then(
      (answer) => { if (current) setCountertops({ status: 'ready', rows: answer.items, pagesWithoutCountertop: answer.pages_without_countertop ?? [] }); },
      (error: unknown) => {
        if (!current) return;
        const message = error instanceof Error ? error.message : String(error);
        setCountertops((previous) => (previous.status === 'ready' ? previous : { status: 'error', error: message }));
      },
    );
    return () => { current = false; };
  }, [packageId, countertopsVersion]);

  async function refreshResults() {
    if (refreshPending.current) return;
    refreshPending.current = true;
    setRefreshing(true);
    setReadiness(null);
    try {
      const detail = await getPackage(projectId(), packageId);
      const [fresh, ready] = await Promise.all([
        loadFindings(projectId(), packageId, detail.current_revision_id),
        getApprovalReadiness(projectId(), packageId),
      ]);
      setFindings(fresh); setRecordedStatus(detail.state as PackageStatus);
      setCountertopsVersion(n => n + 1);
      const changed = fresh.map(f => f.id).sort().join(',') !== checksBaseline.current;
      const finished = changed && detail.state === 'AWAITING_REVIEW';
      const failed = detail.state.startsWith('FAILED') || detail.state === 'CANCELLED';
      if (!waitingForChecks || finished || failed) setReadiness(ready);
      if (finished || failed) setWaitingForChecks(false);
      setResultsVersion(n => n + 1);
    } catch (error) {
      setActionError(`Saved results could not be refreshed. Sign-off is disabled until they load: ${error instanceof Error ? error.message : String(error)}`);
    } finally { refreshPending.current = false; setRefreshing(false); }
  }

  function checksQueued() {
    setValuesChangedSinceRun(false);
    checksBaseline.current = findings.map(f => f.id).sort().join(',');
    setReadiness(null); setWaitingForChecks(true); setActiveTab('results');
  }

  function openRow(rowId: string) {
    setTargetRow(rowId); setMeasureVisited(true); setActiveTab('measure');
  }

  // Where the queue opens: at one item (the Results table's "Confirm the pairing…", #1085), or at the
  // first open one.
  const [queueStart, setQueueStart] = useState<string | null>(null);
  function openQueue(startAt?: string) {
    setQueueStart(startAt ?? null);
    setQueueOpening(queueOpening + 1);
    setQueueOpen(true);
  }

  function openViewer(target: ViewerTarget) {
    setViewer({ opening: openings + 1, target });
    setOpenings(openings + 1);
  }

  /** A finding on its drawing (#1045): as its countertop when it has one, so the page strip and the picture come too. */
  function showDrawing(finding: Finding) {
    const row = countertops.status === 'ready' ? countertops.rows.find((r) => r.finding_id === finding.id) : undefined;
    openViewer(row ? targetFromRow(row) : targetFromFinding(finding));
  }

  /** A countertop row on its drawing page (#1039, #1045). */
  function showRowOnDrawing(row: CountertopResult) {
    openViewer(targetFromRow(row));
  }

  /**
   * "Mark not checkable" on several findings at once (#1039): one note, the same payload rule and
   * the same endpoint as a single decision, one call per finding, then one refresh.
   */
  async function handleBulkDismiss(ids: string[], note: string): Promise<BulkResult> {
    setActionError(null);
    let current: ReviewSession;
    try {
      current = await ensureSession();
    } catch (error) {
      return { saved: 0, failed: ids.map((id) => ({ id, error: error instanceof Error ? error.message : String(error) })) };
    }
    const result: BulkResult = await recordEach(ids, async (id) => {
      const finding = findings.find((f) => f.id === id);
      if (!finding) throw new Error('This finding is no longer in the current result list.');
      await recordReviewAction(projectId(), current.id, decisionPayload(id, finding.outcome, 'dismiss', note));
    });
    setReadiness(null);
    await refreshResults();
    return result;
  }

  /**
   * The sitting these decisions belong to, opened on the first one rather than on arrival.
   *
   * Opening it when the page loads would mint a session every time somebody glanced at a package,
   * and a session is a record that a review happened. Looking is not reviewing.
   */
  async function ensureSession(): Promise<ReviewSession> {
    if (session !== null && session.completed_at === null) return session;
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
          Nothing here has been checked. Do not read an empty list as a package with no findings.
        </p>
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
              setMessages(prev => prev.map(m => m.id === replyId
                ? withStreamStage(m, stage.stage)
                : m));
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
        onShowDrawing={() => showDrawing(finding)}
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
          onShowDrawing={() => showDrawing(enriched)}
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
          onShowDrawing={() => showDrawing(finding)}
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
   * Count a decision only after the server acknowledges it. A stale-list rollback on failure can
   * erase another finding's decision that succeeded in the meantime.
   */
  async function handleAction(
    findingId: string,
    action: SimpleReviewAction,
    note?: string,
  ): Promise<DecisionSaveResult> {
    setActionError(null);
    const finding = findings.find(f => f.id === findingId);
    if (!finding) return { saved: false, error: 'This finding is no longer in the current result list. Refresh it.' };
    setReadiness(null);
    const result = await recordReviewDecision(findingId, action, async () => {
      const current = await ensureSession();
      await recordReviewAction(projectId(), current.id, decisionPayload(findingId, finding.outcome, action, note));
    }, setFindings);
    await refreshResults();
    return result;
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
  async function handleCorrect(findingId: string, correctedValue: string): Promise<DecisionSaveResult> {
    setActionError(null);
    try {
      const current = await ensureSession();
      const chain = await getFindingChain(projectId(), packageId, findingId);
      // Through `evidence`, which is where the chain puts the observation an operand came from —
      // and `null` there is meaningful: an operand a reviewer supplied has no observation behind it,
      // so there is nothing to correct rather than something to correct blindly.
      const observationIds = [...new Set(chain.operands?.flatMap((operand) =>
        operand.evidence ? [operand.evidence.canonical_observation_id] : [],
      ) ?? [])];
      if (observationIds.length > 1) {
        return { saved: false, error: 'This finding uses several drawing readings. Inspect its evidence and correct the specific measurement in Measurements before running the checks again.' };
      }
      const observationId = observationIds[0] ?? null;
      if (observationId === null) {
        return { saved: false, error: 'This finding does not name a reading that can be corrected — it has no authoritative ' +
          'observation behind it, so there is nothing to correct.' };
      }
      await decideEvidence(projectId(), current.id, {
        finding_id: findingId,
        observation_id: observationId,
        action: 'correct',
        corrected_value: correctedValue,
      });
      setFindings(prev =>
        prev.map(f => (f.id === findingId ? { ...f, reviewer_action: 'correct', reviewer_carried_over: false } : f)),
      );
      await refreshResults();
      return { saved: true };
    } catch (error) {
      return { saved: false, error: `That correction was not recorded — ${error instanceof Error ? error.message : String(error)}` };
    }
  }

  /**
   * Accept one deviation, until a date.
   *
   * The scope is this finding and nothing wider. A reviewer saying "this one is acceptable" is not
   * the same as saying the rule should stop firing, and the second is a rule change that goes
   * through the rulebook where somebody reviews it.
   */
  async function handleExcept(findingId: string, reason: string, expiresAt: string): Promise<DecisionSaveResult> {
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
        prev.map(f => (f.id === findingId ? { ...f, reviewer_action: 'except', reviewer_carried_over: false } : f)),
      );
      await refreshResults();
      return { saved: true };
    } catch (error) {
      return { saved: false, error: `That exception was not granted — ${error instanceof Error ? error.message : String(error)}` };
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
  /** Returns null once the approval is recorded, or why nothing was signed (#1064: the dialog says it). */
  async function handleSignOff(): Promise<string | null> {
    if (!canSignOff(readiness) || isSigningOff || isApproved) {
      return readiness?.reason ?? 'the review is not ready to sign off right now.';
    }
    setActionError(null);
    setIsSigningOff(true);
    try {
      const current = await ensureSession();
      await approvePackage(projectId(), current.id);
      setApproved(true); // The approval was acknowledged, even if a subsequent read fails.
      onPackageChanged?.();
      // Re-read rather than assume: approval completes the sitting server-side, and the package
      // state a moment ago is not the one the download button should be reading.
      try {
        const [detail, sitting] = await Promise.all([
          getPackage(projectId(), packageId), completeSessionState(current.id),
        ]);
        setRecordedStatus(detail.state as PackageStatus);
        setApproved(detail.state === 'APPROVED');
        setSession(sitting);
      } catch (error) {
        setActionError(`Sign-off was recorded, but its refreshed status could not be loaded. Reload to check it: ${error instanceof Error ? error.message : String(error)}`);
      }
      return null;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      setActionError(`Sign-off did not complete — ${message}`);
      return message;
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
    if (downloadPending.current) return;
    downloadPending.current = true;
    setDownloadState({ status: 'loading', format });
    try {
      await receiveReport(format, (requested) => requested === 'pdf'
        ? downloadPdfReport(projectId(), packageId)
        : requested === 'redline'
          ? downloadRedline(projectId(), packageId)
          : downloadReport(projectId(), packageId), (blob) => {
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `gv-review-${packageId}${format === 'redline' ? '-redline' : ''}.${format === 'workbook' ? 'xlsx' : 'pdf'}`;
      link.click();
      URL.revokeObjectURL(url);
      });
      setDownloadState({ status: 'started', format });
    } catch (error) {
      setDownloadState({ status: 'error', format, message: error instanceof Error ? error.message : String(error) });
    } finally {
      downloadPending.current = false;
    }
  }

  const pkg = {
    id: packageId,
    vendor: remote.status === 'ready' ? (remote.data.detail.vendor ?? '—') : '—',
    status: recordedStatus ?? (remote.status === 'ready'
      ? remote.data.detail.state
      : 'CREATED') as PackageStatus,
    // The project id, until the API carries a human project name. An id a reviewer can quote beats
    // a friendly label that is not in any record.
    project: remote.status === 'ready' ? remote.data.detail.project_id : '',
    revision: remote.status === 'ready' ? remote.data.detail.current_revision_number : null,
  };
  const stage = reviewStage({
    state: pkg.status,
    findingsTotal: findings.length,
    checksQueued: waitingForChecks,
    valuesChangedSinceRun,
    readiness: readiness ? { blockingFindings: readiness.blocking_findings, canApprove: readiness.can_approve, reason: readiness.reason } : null,
    exports: exportsStatus,
  });
  // Once approved (even if the page could not re-read the package), nothing offers Sign off again.
  const signOffBlocked = isSigningOff || !canSignOff(readiness) || waitingForChecks || isApproved;

  // What a sign-off covers (#1064), from what the page already loaded: the countertops by who settled
  // them, the other (package-level) results, and the total the server approves (the live run's).
  const countertopsReady = countertops.status === 'ready';
  const countertopFindingIds = new Set(countertopsReady ? countertops.rows.flatMap((row) => (row.finding_id ? [row.finding_id] : [])) : []);
  // The architect comparison (#1085) is a countertop's own, said on its own line, not under "Other checks".
  const architectIds = countertopsReady ? architectFindingIds(countertops.rows) : new Set<string>();
  const signOffScope: SignOffScope = {
    countertops: countertopsReady ? signOffSummary(countertops.rows) : null,
    countertopsFailed: countertops.status === 'error',
    architect: countertopsReady ? findings.filter((finding) => architectIds.has(finding.id)).length : null,
    otherChecks: countertopsReady ? findings.filter((finding) => !countertopFindingIds.has(finding.id) && !architectIds.has(finding.id)).length : null,
    total: findings.length,
  };
  const signer = session?.reviewer ?? (remote.status === 'ready' ? remote.data.me : null);

  function openSignOff() {
    setSignOffError(null);
    setSignOffOpen(true);
  }

  /** The dialog closes only once the approval is recorded; otherwise it says why nothing was signed. */
  async function confirmSignOff() {
    const failure = await handleSignOff();
    if (failure !== null) {
      setSignOffError(failure);
      return;
    }
    setSignOffOpen(false);
    // The Sign off panel and its button are gone; take focus to the Report panel that replaced them.
    window.setTimeout(() => document.getElementById('report-panel-title')?.focus(), 50);
  }
  // The Report panel sits on Results after sign-off and says its own receipts there.
  const reportPanelShown = activeTab === 'results' && isApproved;

  /** The header's one action. Each kind runs the same handler the page already had. */
  function act(kind: NextActionKind) {
    if (kind === 'run-checks') {
      // Run checks lives on the Measurements form, which saves the visible values first. Take the
      // reviewer there rather than queue a run that skips that save.
      setMeasureVisited(true);
      setActiveTab('measure');
      window.setTimeout(() => {
        const button = document.getElementById('measure-run-checks');
        button?.scrollIntoView({ block: 'center' });
        button?.focus();
      }, 50);
    } else if (kind === 'review') {
      setActiveTab('results');
      openQueue();
    } else if (kind === 'sign-off') {
      // Never on one click: the confirmation names the signer and what is signed (#1064).
      if (!signOffBlocked) openSignOff();
    } else if (kind === 'prepare-report') {
      void prepareReport();
    } else if (kind === 'download-report') {
      void handleDownload('pdf');
    }
  }

  async function prepareReport() {
    setPreparingExports(true);
    setPrepareError(null);
    try {
      await prepareSignedExports(projectId(), packageId);
      setExportsVersion((n) => n + 1);
    } catch (error) {
      setPrepareError(error instanceof Error ? error.message : String(error));
    } finally {
      setPreparingExports(false);
    }
  }

  const secondary: SecondaryAction[] = [
    { id: 'refresh', label: refreshing ? 'Refreshing results…' : 'Refresh results', disabled: refreshing, onSelect: () => void refreshResults() },
    ...(stage.next.kind === 'download-report'
      ? [
          { id: 'workbook', label: 'Download workbook', disabled: downloadState.status === 'loading', onSelect: () => void handleDownload('workbook') },
          { id: 'redline', label: 'Download redline', disabled: downloadState.status === 'loading', onSelect: () => void handleDownload('redline') },
        ]
      : []),
    ...(isApproved && stage.next.kind !== 'download-report'
      ? [{ id: 'report-status', label: 'Check report status', onSelect: () => setExportsVersion((n) => n + 1) }]
      : []),
    { id: 'project-values', label: 'Project values', onSelect: () => setProjectValuesOpen(true) },
    { id: 'record-ids', label: 'Record IDs', onSelect: () => setRecordIdsOpen(true) },
  ];

  return (
    <div className="review-page">
      <HeaderTitleExtra>
        <PackageStatusBadge status={pkg.status} />
      </HeaderTitleExtra>
      <HeaderActions>
        <Button variant="ghost" size="sm" onClick={() => setChatOpen(true)} aria-label="Open chat">
          <MessageSquare /> <span className="hidden xl:inline">Chat</span>
        </Button>
        {/* From tablet width up; on a phone the same table opens from "More actions". */}
        <span className="hidden md:inline-flex">
          <ChangedValuesBadge
            state={changedValues.status}
            value={changedValues.status === 'ready' ? changedValues.data : null}
            currentRevisionId={remote.status === 'ready' ? remote.data.detail.current_revision_id : null}
          />
        </span>
        <NextActionButton
          action={stage.next}
          onAct={act}
          busyLabel={stage.next.kind === 'sign-off' && isSigningOff ? 'Signing off…' : stage.next.kind === 'prepare-report' && preparingExports ? 'Requesting…' : null}
          extraDisabled={stage.next.kind === 'sign-off' ? signOffBlocked : stage.next.kind === 'download-report' ? downloadState.status === 'loading' : stage.next.kind === 'prepare-report' ? preparingExports : false}
          secondary={secondary}
        />
      </HeaderActions>
      <Dialog open={projectValuesOpen} onOpenChange={setProjectValuesOpen}>
        <DialogContent className="sm:max-w-xl">
          <DialogTitle className="sr-only">Project values</DialogTitle>
          <ChangedValuesPanel
            state={changedValues.status}
            value={changedValues.status === 'ready' ? changedValues.data : null}
            currentRevisionId={remote.status === 'ready' ? remote.data.detail.current_revision_id : null}
          />
        </DialogContent>
      </Dialog>
      <SignOffDialog
        open={signOffOpen}
        onOpenChange={setSignOffOpen}
        signer={signer}
        vendor={pkg.vendor}
        revision={pkg.revision}
        scope={signOffScope}
        ready={canSignOff(readiness) && !waitingForChecks && !isApproved}
        busy={isSigningOff}
        error={signOffError}
        onConfirm={() => void confirmSignOff()}
      />
      <RecordIdsDialog
        open={recordIdsOpen}
        onOpenChange={setRecordIdsOpen}
        packageId={pkg.id}
        projectId={pkg.project}
        revisionId={remote.status === 'ready' ? remote.data.detail.current_revision_id : null}
      />

      <ReviewStepper steps={stage.steps} />

      {/* Download receipts and export problems, said where the reviewer is looking. */}
      {!reportPanelShown && downloadState.status === 'loading' && <p className="mx-4 mt-2 text-sm sm:mx-6" role="status">Requesting {downloadState.format} report…</p>}
      {!reportPanelShown && downloadState.status === 'started' && <p className="mx-4 mt-2 text-sm sm:mx-6" role="status">Download started. Check your browser downloads.</p>}
      {!reportPanelShown && downloadState.status === 'error' && <p className="mx-4 mt-2 text-sm sm:mx-6" role="alert">The report could not be downloaded: {downloadState.message}</p>}
      {!reportPanelShown && exportsError !== null && <p className="mx-4 mt-2 text-sm sm:mx-6" role="alert">Could not check the signed reports: {exportsError}</p>}

      {/* A write that failed, said out loud. The row has already been put back, so without this the
          reviewer would see their tick disappear and have no idea why — and might reasonably assume
          they had mis-clicked rather than that nothing was saved. */}
      {actionError !== null && (
        <div className="upload-error" role="alert">
          <strong>Review update</strong>
          <p>{actionError}</p>
        </div>
      )}

      <div data-tw className="flex items-center gap-2 border-b bg-background px-4 py-2 font-sans sm:px-6">
        <Tabs
          value={activeTab}
          onValueChange={(value) => {
            if (value === 'measure') setMeasureVisited(true);
            setActiveTab(value as 'results' | 'measure');
          }}
        >
          <TabsList>
            <TabsTrigger value="results">
              Results
              {readiness !== null && readiness.blocking_findings > 0 && (
                <span className="num rounded-full bg-outcome-review-bg px-1.5 text-xs text-outcome-review-fg">{readiness.blocking_findings}</span>
              )}
            </TabsTrigger>
            <TabsTrigger value="measure">Measurements</TabsTrigger>
          </TabsList>
        </Tabs>
      </div>

      {activeTab === 'results' && (
        <div className="min-h-0 flex-1 overflow-y-auto">
          {stage.current === 'signoff' && !isApproved && (
            <SignOffPanel
              readiness={readiness}
              ready={canSignOff(readiness) && !waitingForChecks}
              scope={signOffScope}
              busy={isSigningOff}
              onSignOff={openSignOff}
              onReview={() => openQueue()}
            />
          )}
          {isApproved && (
            <ReportPanel
              status={exportsStatus}
              error={exportsError}
              prepareError={prepareError}
              requesting={preparingExports}
              download={downloadState}
              onPrepare={() => void prepareReport()}
              onCheck={() => setExportsVersion((n) => n + 1)}
              onDownload={(format) => void handleDownload(format)}
            />
          )}
          <ResultsDashboard
            countertops={countertops}
            findings={findings}
            blockingIds={readiness ? new Set(readiness.blocking_finding_ids) : null}
            busy={refreshing}
            filter={resultsFilter}
            onFilterChange={setResultsFilter}
            onRetry={() => { setCountertops({ status: 'loading' }); setCountertopsVersion((n) => n + 1); }}
            onRefresh={() => void refreshResults()}
            handlers={{ onAction: handleAction, onCorrect: handleCorrect, onExcept: handleExcept }}
            onBulkDismiss={handleBulkDismiss}
            onShowDrawing={showRowOnDrawing}
            onOpenQueue={openQueue}
            onOpenCard={(row) => openRow(row.row_id)}
          />
        </div>
      )}

      {/* Chat, beside the results rather than instead of them (#1039). A legacy island: the chat
          keeps its own styles inside the shadcn sheet. */}
      <Sheet open={chatOpen} onOpenChange={setChatOpen}>
        <SheetContent side="right" className="flex w-full flex-col gap-0 p-0 sm:max-w-xl">
          <SheetHeader className="border-b">
            <SheetTitle>Chat</SheetTitle>
            <SheetDescription>Ask about this review in plain words.</SheetDescription>
          </SheetHeader>
          <div data-legacy className="flex min-h-0 flex-1 flex-col bg-[var(--bg-base)]">
            <ChatThread
              messages={messages.map(m => ({
                ...m,
                findings: m.findings?.map(f => findings.find(rf => rf.id === f.id) ?? f),
              }))}
              selectedFinding={selectedFindingId}
              recordedFindingCount={findings.length}
              blockingFindingIds={readiness?.blocking_finding_ids}
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
          </div>
        </SheetContent>
      </Sheet>

      {openings > 0 && (
        <Suspense fallback={null}>
          <DrawingViewerSheet
            target={viewer?.target ?? null}
            opening={viewer?.opening ?? 0}
            rows={countertops.status === 'ready' ? countertops.rows : []}
            projectId={projectId()}
            packageId={packageId}
            onTargetChange={(target) => setViewer((current) => (current ? { ...current, target } : current))}
            onClose={() => setViewer(null)}
          />
        </Suspense>
      )}
      {queueOpening > 0 && (
        <Suspense fallback={null}>
          <NeedsYouQueue
            open={queueOpen}
            opening={queueOpening}
            onOpenChange={setQueueOpen}
            rows={countertops.status === 'ready' ? countertops.rows : []}
            rowsReady={countertops.status === 'ready'}
            findings={findings}
            blocking={readiness ? new Set(readiness.blocking_finding_ids) : null}
            projectId={projectId()}
            packageId={packageId}
            handlers={{ onAction: handleAction, onCorrect: handleCorrect, onExcept: handleExcept }}
            next={stage.next}
            onAct={act}
            onWallSaved={() => {
              // A wall answer changes a check input: the header asks for a run, and the rows reload.
              if (findings.length > 0) setValuesChangedSinceRun(true);
              setCountertopsVersion((n) => n + 1);
            }}
            onPairingSaved={() => {
              // So does an architect pairing (#1085): only a new check run uses it.
              if (findings.length > 0) setValuesChangedSinceRun(true);
            }}
            onOpenCard={(row) => openRow(row.row_id)}
            startAt={queueStart}
          />
        </Suspense>
      )}
      {measureVisited && (
        <div className="review-page__measure-container" hidden={activeTab !== 'measure'}>
          <MeasurementPanel
            packageId={packageId}
            onChoosePackage={onBackToDocuments}
            onDone={() => { setActiveTab('results'); void refreshResults(); }}
            onChecksQueued={checksQueued}
            onValuesSaved={() => { if (findings.length > 0) setValuesChangedSinceRun(true); }}
            targetRow={activeTab === 'measure' ? targetRow : null}
            onTargetReached={() => setTargetRow(null)}
            onReviewRow={(rowId) => { setResultsFilter('all'); setActiveTab('results'); setTimeout(() => { const row = document.querySelector<HTMLElement>(`[data-slot="countertop-table"] tr[data-row-id="${rowId}"]`); row?.scrollIntoView({ block: 'center' }); row?.focus(); }, 80); }}
            onOpenQueue={() => openQueue()}
          />
        </div>
      )}
    </div>
  );
}

function ReviewSkeleton() {
  return (
    <div className="review-page review-page--loading" data-tw aria-busy="true" aria-label="Loading the review">
      <div className="flex items-center gap-3 border-b px-4 py-3 sm:px-6">
        {[0, 1, 2, 3, 4, 5].map((step) => (
          <div key={step} className="flex flex-1 items-center gap-2">
            <Skeleton className="size-6 shrink-0 rounded-full" />
            <Skeleton className="hidden h-3 w-16 lg:block" />
          </div>
        ))}
      </div>
      <div className="flex flex-col gap-3 px-4 py-6 sm:px-6">
        <Skeleton className="h-5 w-40" />
        <Skeleton className="h-4 w-64" />
        {[0, 1, 2].map((row) => (
          <Skeleton key={row} className="h-24 w-full" />
        ))}
      </div>
    </div>
  );
}
