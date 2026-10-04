import { useEffect, useRef, useState } from 'react';
import { CheckCircle2, XCircle, AlertCircle, CircleDashed, Sparkles, Shield } from 'lucide-react';
import { OUTCOME_LABELS } from '../../data/outcomeLabels.js';
import type { ChatMessage, Finding } from '../../data/types';
import { FindingCard } from './FindingCard';
import { ThinkingStream } from './ThinkingStream';
import { StreamingText } from './StreamingText';
import { ChatMarkdown } from './ChatMarkdown';
import { FindingsTable } from '../output/FindingsTable.js';
import './ChatThread.css';

interface ChatThreadProps {
  messages: ChatMessage[];
  selectedFinding: string | null;
  recordedFindingCount: number;
  onViewEvidence: (finding: Finding) => void;
  onAction: (findingId: string, action: 'confirm' | 'correct' | 'except' | 'dismiss', note?: string) => void;
  onCorrect: (findingId: string, correctedValue: string) => void;
  onExcept: (findingId: string, reason: string, expiresAt: string) => void;
}

export function ChatThread({
  messages,
  selectedFinding,
  recordedFindingCount,
  onViewEvidence,
  onAction,
  onCorrect,
  onExcept,
}: ChatThreadProps) {
  const bottomRef = useRef<HTMLDivElement>(null);
  const threadRef = useRef<HTMLDivElement>(null);

  /**
   * Which reply is currently being revealed.
   *
   * Only a message that has just arrived streams. Without this, every answer in the thread would
   * replay its reveal on any re-render — scrolling the history would look like the system was
   * re-answering questions it had already answered.
   */
  const [streamingId, setStreamingId] = useState<string | null>(null);
  const seen = useRef<Set<string>>(new Set());

  useEffect(() => {
    const last = messages[messages.length - 1];
    if (last && last.role === 'assistant' && !last.is_typing && !seen.current.has(last.id)) {
      setStreamingId(last.id);
    }
    messages.forEach((message) => seen.current.add(message.id));
  }, [messages]);

  /**
   * Follow the bottom of the thread as it grows — but only while the reviewer is already there.
   *
   * A `ResizeObserver` rather than an effect on `messages`, because the thread also grows between
   * message changes: a reply reveals itself word by word, and finding cards expand. Watching the
   * content box catches every one of those without the components having to report their own size.
   *
   * The 120px threshold is what keeps it from fighting the reader. Somebody who has scrolled up to
   * re-read an earlier finding is not moved; an answer arriving while they are at the bottom is
   * followed.
   */
  useEffect(() => {
    const thread = threadRef.current;
    if (!thread) return;

    const nearBottom = () =>
      thread.scrollHeight - thread.scrollTop - thread.clientHeight < 120;

    // Read from the live scroll position, not hardcoded. This effect re-runs whenever a message
    // arrives, so `let pinned = true` re-pinned the reader on every reply — a reviewer scrolled up
    // to re-read an earlier finding was yanked to the bottom, which is the exact behaviour the
    // comment above says this avoids.
    let pinned = nearBottom();
    const onScroll = () => { pinned = nearBottom(); };
    thread.addEventListener('scroll', onScroll, { passive: true });

    const observer = new ResizeObserver(() => {
      if (pinned) bottomRef.current?.scrollIntoView({ block: 'end' });
    });
    Array.from(thread.children).forEach((child) => observer.observe(child));

    return () => {
      thread.removeEventListener('scroll', onScroll);
      observer.disconnect();
    };
  }, [messages.length]);

  return (
    <div className="chat-thread" role="log" aria-live="polite" ref={threadRef}>
      {messages.length === 0 && (
        <div className="chat-thread__empty">
          <span className="chat-thread__empty-mark" aria-hidden="true">GV</span>
          <h2>Ask about this review</h2>
          <p>
            {recordedFindingCount > 0
              ? `${recordedFindingCount} recorded ${recordedFindingCount === 1 ? 'finding is' : 'findings are'} ready to explore. Ask which checks need attention.`
              : 'No findings are recorded for this package yet. Enter measurements and run the checks to create them.'}
          </p>
        </div>
      )}
      {messages.map((msg) => (
        <div key={msg.id} className={`chat-message chat-message--${msg.role}`}>
          {/* Content */}
          <div className="chat-message__content">
            <div className="chat-message__header">
              <span className="chat-message__sender">
                {msg.role === 'assistant' ? 'GV Review' : 'You'}
              </span>
              {!msg.is_typing && (
                <time className="chat-message__time sr-only" dateTime={msg.timestamp}>
                  {formatTime(msg.timestamp)}
                </time>
              )}
            </div>

            {/* In-flight state, or the answer. */}
            {msg.is_typing ? (
              <ThinkingStream />
            ) : (
              <div className="chat-message__text">
                <StreamingText
                  text={msg.content}
                  stream={msg.id === streamingId}
                  render={(text) => <ChatMarkdown text={text} />}
                />
              </div>
            )}

            {/* Real progress, from the server: a model is writing about the findings below. It is
                shown only between the stream's facts and its guarded narration, never on a timer. */}
            {msg.role === 'assistant' && msg.narrating && (
              <p className="chat-message__narrating" role="status" aria-live="polite">
                <span className="chat-message__narrating-dot" aria-hidden="true" />
                {msg.streamStage === 'narrating' ? 'Writing the explanation' : 'Preparing the explanation'}
              </p>
            )}
            {msg.role === 'assistant' && msg.narration && (
              <NarrationBadge narration={msg.narration} />
            )}

            {/* Inline findings */}
            {msg.findings && msg.findings.length > 0 && (
              <div className="chat-message__findings">
                <FindingsSummary findings={msg.findings} />

                {/* One table, not one card per finding. Each card, with its evidence and actions,
                    opens under its own row. */}
                <FindingsTable
                  findings={msg.findings}
                  narratives={msg.narratives}
                  selectedFinding={selectedFinding}
                  onViewEvidence={onViewEvidence}
                  renderDetail={(finding) => (
                    <FindingCard
                      finding={finding}
                      isSelected={selectedFinding === finding.id}
                      onViewEvidence={onViewEvidence}
                      onAction={onAction}
                      onCorrect={onCorrect}
                      onExcept={onExcept}
                    />
                  )}
                />
              </div>
            )}
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

/**
 * Whether this answer's prose came from the model or from the deterministic fallback.
 *
 * Kept prominent rather than tucked into a footnote. Under `AGENTS.md` §2 the verdicts are the
 * engine's in both modes, and a reviewer is entitled to know which of the two wrote the sentences
 * they are reading before they weigh them.
 */
function NarrationBadge({ narration }: { narration: NonNullable<ChatMessage['narration']> }) {
  const isLlm = narration.mode === 'llm';
  // One line. The disclosure that matters to a reviewer is which source wrote the prose. The raw
  // fallback reason is a developer's diagnostic (it can be a Pydantic error quoting its own docs),
  // so it is kept on the badge as a data attribute for somebody inspecting the page, and is never
  // shown or announced.
  const detail = isLlm
    ? `Written by ${narration.modelId ?? 'the configured model'}, checked against the recorded findings before it was shown.`
    : 'No model narrated this answer. The findings are the recorded ones, unchanged.';
  return (
    <div
      className={`narration narration--${narration.mode}`}
      title={detail}
      data-fallback-reason={narration.fallbackReason ?? undefined}
    >
      <span className="narration__icon" aria-hidden="true">
        {isLlm ? <Sparkles size={12} /> : <Shield size={12} />}
      </span>
      <span className="narration__body">
        <strong>{isLlm ? 'AI narration' : 'Deterministic findings'}</strong>
        {isLlm && narration.modelId && <code>{narration.modelId}</code>}
        <span className="sr-only">{detail}</span>
      </span>
    </div>
  );
}

/**
 * The outcome breakdown for one run, with the share of checks the engine could decide.
 *
 * **The percentage is resolution, and it is labelled as resolution — not as accuracy.** It is
 * `(PASS + FAIL) / total`, computed from the outcomes on screen. Accuracy would be a comparison
 * against reviewed answers, and there are none yet (`eval/` has the harness; the gold set is
 * unbuilt), so a number called accuracy here would have nothing behind it.
 *
 * The complement is shown as what it is rather than hidden: an abstention is a result. A check that
 * says "a person needs to look at this" is the system working correctly, and a meter that counted
 * it as a shortfall would be scoring the product for being careful.
 */
function FindingsSummary({ findings }: { findings: Finding[] }) {
  const counts = {
    PASS: findings.filter(f => f.outcome === 'PASS').length,
    FAIL: findings.filter(f => f.outcome === 'FAIL').length,
    REVIEW_REQUIRED: findings.filter(f => f.outcome === 'REVIEW_REQUIRED').length,
    NOT_FOUND: findings.filter(f => f.outcome === 'NOT_FOUND').length,
  };
  const decided = counts.PASS + counts.FAIL;
  const total = findings.length;
  const share = total > 0 ? Math.round((decided / total) * 100) : 0;

  return (
    <div className="summary">
      <div className="summary__head">
        <span className="summary__title">{total} checks run</span>
        <span className="summary__share mono">
          {share}%<span className="summary__share-label">decided</span>
        </span>
      </div>

      {/* One track, segmented by outcome. Proportional to the real counts. */}
      <div
        className="summary__meter"
        role="img"
        aria-label={`${counts.PASS} ${OUTCOME_LABELS.PASS}, ${counts.FAIL} ${OUTCOME_LABELS.FAIL}, ${counts.REVIEW_REQUIRED} ${OUTCOME_LABELS.REVIEW_REQUIRED}, ${counts.NOT_FOUND} ${OUTCOME_LABELS.NOT_FOUND}`.toLowerCase()}
      >
        {(['PASS', 'FAIL', 'REVIEW_REQUIRED', 'NOT_FOUND'] as const).map((outcome) =>
          counts[outcome] > 0 ? (
            <span
              key={outcome}
              className={`summary__seg summary__seg--${outcome.toLowerCase()}`}
              style={{ flexGrow: counts[outcome] }}
            />
          ) : null,
        )}
      </div>

      <div className="summary__counts">
        {/* "not found" read as missing evidence; it means a value the check needs was never supplied. */}
        {counts.PASS > 0 && <Count icon={<CheckCircle2 size={12} />} kind="pass" n={counts.PASS} label={OUTCOME_LABELS.PASS.toLowerCase()} />}
        {counts.FAIL > 0 && <Count icon={<XCircle size={12} />} kind="fail" n={counts.FAIL} label={OUTCOME_LABELS.FAIL.toLowerCase()} />}
        {counts.REVIEW_REQUIRED > 0 && <Count icon={<AlertCircle size={12} />} kind="review" n={counts.REVIEW_REQUIRED} label={OUTCOME_LABELS.REVIEW_REQUIRED.toLowerCase()} />}
        {counts.NOT_FOUND > 0 && <Count icon={<CircleDashed size={12} />} kind="missing" n={counts.NOT_FOUND} label={OUTCOME_LABELS.NOT_FOUND.toLowerCase()} />}
      </div>
    </div>
  );
}

function Count({ icon, kind, n, label }: { icon: React.ReactNode; kind: string; n: number; label: string }) {
  return (
    <span className={`summary__count summary__count--${kind}`}>
      {icon}
      <strong>{n}</strong> {label}
    </span>
  );
}

// ── Helpers ──────────────────────────────────────────────────
function formatTime(iso: string) {
  return new Date(iso).toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit',
  });
}
