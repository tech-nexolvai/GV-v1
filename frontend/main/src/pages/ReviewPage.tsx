import { useState, useEffect } from 'react';
import { ChatThread } from '../components/chat/ChatThread';
import { ChatInput } from '../components/chat/ChatInput';
import { EvidencePanel } from '../components/chat/EvidencePanel';
import { StatusBadge } from '../components/ui/Badge';
import type { Finding, ChatMessage } from '../data/mock';
import { MOCK_SESSIONS, MOCK_PACKAGE } from '../data/mock';
import { FileText, CheckSquare } from 'lucide-react';
import './ReviewPage.css';

interface ReviewPageProps {
  sessionId: string;
  onEvidenceChange: (panel: React.ReactNode) => void;
}

export function ReviewPage({ sessionId, onEvidenceChange }: ReviewPageProps) {
  // Find current session
  const initialSession = MOCK_SESSIONS.find(s => s.id === sessionId) || MOCK_SESSIONS[0];

  const [messages, setMessages] = useState<ChatMessage[]>(initialSession.messages);
  const [selectedFindingId, setSelectedFindingId] = useState<string | null>(null);
  
  // Extract findings from assistant message inside active session
  const initialFindings = initialSession.messages.find(m => m.role === 'assistant' && m.findings)?.findings || [];
  const [findings, setFindings] = useState<Finding[]>(initialFindings);
  const [isProcessing, setIsProcessing] = useState(false);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    setIsLoading(true);
    const timer = setTimeout(() => {
      setIsLoading(false);
    }, 600);
    return () => clearTimeout(timer);
  }, [sessionId]);

  if (isLoading) {
    return <ReviewSkeleton />;
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

  const pkg = MOCK_PACKAGE;
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
