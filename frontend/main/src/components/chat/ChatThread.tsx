import { useRef, useEffect } from 'react';
import { User } from 'lucide-react';
import type { ChatMessage, Finding } from '../../data/types';
import { FindingCard } from './FindingCard';
import './ChatThread.css';

interface ChatThreadProps {
  messages: ChatMessage[];
  selectedFinding: string | null;
  onViewEvidence: (finding: Finding) => void;
  onAction: (findingId: string, action: 'confirm' | 'correct' | 'except' | 'dismiss', note?: string) => void;
}

export function ChatThread({ messages, selectedFinding, onViewEvidence, onAction }: ChatThreadProps) {
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [messages]);

  return (
    <div className="chat-thread" role="log" aria-live="polite">
      {messages.map((msg, msgIdx) => (
        <div
          key={msg.id}
          className={`chat-message chat-message--${msg.role} animate-slide-up`}
          style={{ animationDelay: `${msgIdx * 60}ms` }}
        >
          {/* Avatar */}
          <div className="chat-message__avatar" aria-hidden="true">
            {msg.role === 'assistant'
              ? <div className="chat-message__avatar-gv"><span>GV</span></div>
              : <div className="chat-message__avatar-user"><User size={13} /></div>
            }
          </div>

          {/* Content */}
          <div className="chat-message__content">
            <div className="chat-message__header">
              <span className="chat-message__sender">
                {msg.role === 'assistant' ? 'GV Review' : 'You'}
              </span>
              <time className="chat-message__time" dateTime={msg.timestamp}>
                {formatTime(msg.timestamp)}
              </time>
            </div>

            {/* Text */}
            <div className="chat-message__text">
              {renderMarkdown(msg.content)}
            </div>

            {/* Inline findings */}
            {msg.findings && msg.findings.length > 0 && (
              <div className="chat-message__findings">
                {/* Summary row */}
                <div className="chat-message__findings-summary">
                  <FindingsSummary findings={msg.findings} />
                </div>

                {/* Finding cards */}
                <div className="chat-message__findings-list">
                  {msg.findings.map((finding, i) => (
                    <FindingCard
                      key={finding.id}
                      finding={finding}
                      isSelected={selectedFinding === finding.id}
                      onViewEvidence={onViewEvidence}
                      onAction={onAction}
                      animationDelay={i * 80}
                    />
                  ))}
                </div>
              </div>
            )}

            {/* Typing indicator */}
            {msg.is_typing && <TypingIndicator />}
          </div>
        </div>
      ))}
      <div ref={bottomRef} />
    </div>
  );
}

// ── Finding summary row ──────────────────────────────────────
function FindingsSummary({ findings }: { findings: Finding[] }) {
  const counts = {
    PASS: findings.filter(f => f.outcome === 'PASS').length,
    FAIL: findings.filter(f => f.outcome === 'FAIL').length,
    REVIEW_REQUIRED: findings.filter(f => f.outcome === 'REVIEW_REQUIRED').length,
    NOT_FOUND: findings.filter(f => f.outcome === 'NOT_FOUND').length,
  };

  return (
    <div className="findings-summary">
      <span className="findings-summary__label">{findings.length} checks</span>
      <div className="findings-summary__counts">
        {counts.PASS > 0 && <span className="findings-summary__count findings-summary__count--pass">✓ {counts.PASS} pass</span>}
        {counts.FAIL > 0 && <span className="findings-summary__count findings-summary__count--fail">✕ {counts.FAIL} fail</span>}
        {counts.REVIEW_REQUIRED > 0 && <span className="findings-summary__count findings-summary__count--review">◎ {counts.REVIEW_REQUIRED} review</span>}
        {counts.NOT_FOUND > 0 && <span className="findings-summary__count findings-summary__count--missing">○ {counts.NOT_FOUND} missing</span>}
      </div>
    </div>
  );
}

// ── Typing indicator ─────────────────────────────────────────
function TypingIndicator() {
  return (
    <div className="typing-indicator" aria-label="GV Review is processing">
      <span /><span /><span />
    </div>
  );
}

// ── Helpers ──────────────────────────────────────────────────
function formatTime(iso: string) {
  return new Date(iso).toLocaleTimeString('en-US', {
    hour: '2-digit', minute: '2-digit',
  });
}

function renderMarkdown(text: string) {
  // Simple bold + newline support — good enough for the prototype
  const parts = text.split(/(\*\*[^*]+\*\*|\n)/g);
  return (
    <p>
      {parts.map((part, i) => {
        if (part.startsWith('**') && part.endsWith('**'))
          return <strong key={i}>{part.slice(2, -2)}</strong>;
        if (part === '\n') return <br key={i} />;
        return part;
      })}
    </p>
  );
}
