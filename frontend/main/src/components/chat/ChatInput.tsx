import { useEffect, useRef, useState } from 'react';
import { Send } from 'lucide-react';
import './ChatInput.css';

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

/**
 * Grounded prompts for the run-scoped chat. The server supplies only stored deterministic findings
 * and evidence pages to its language layer; these never ask it to run a check or make a verdict.
 */
const QUICK_PROMPTS = [
  'Show all findings',
  'Show FAIL findings',
  'Show findings needing review',
  'Which sheet has the failure?',
  'Why did this fail?',
];

export function ChatInput({ onSend, disabled, placeholder = 'Ask about this package…' }: ChatInputProps) {
  const [value, setValue] = useState('');
  const textarea = useRef<HTMLTextAreaElement>(null);

  /**
   * Grow the box to fit what has been typed, up to the max height the stylesheet sets.
   *
   * Reset to `auto` before reading `scrollHeight`: without that the element never reports a height
   * smaller than it already has, so the box grows as you type and then refuses to shrink when you
   * delete. Capped in CSS rather than here, so the limit lives with the rest of the sizing.
   */
  useEffect(() => {
    const element = textarea.current;
    if (!element) return;
    element.style.height = 'auto';
    element.style.height = `${element.scrollHeight}px`;
  }, [value]);

  function handleSend() {
    const trimmed = value.trim();
    if (!trimmed || disabled) return;
    onSend(trimmed);
    setValue('');
  }

  function handleKeyDown(e: React.KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  return (
    <div className="chat-input-area">
      {/* Quick prompts — labelled as AI suggestions */}
      <div className="chat-input-area__quick">
        <div className="chat-input-area__quick-header">
          <span className="chat-input-area__quick-label">Suggested</span>
          <div className="chat-input-area__quick-sep" />
        </div>
        {QUICK_PROMPTS.map(p => (
          <button
            key={p}
            className="chat-input-area__quick-btn"
            onClick={() => onSend(p)}
            disabled={disabled}
          >
            {p}
          </button>
        ))}
      </div>

      {/* Input row */}
      <div className="chat-input-area__row">
        {/* The attach button was here. It had no handler — a paperclip captioned "Attach drawing"
            that did nothing when clicked, on the screen where a reviewer would most reasonably expect
            to add one. Drawings are submitted through the new-package flow, which does work; an
            affordance that silently does nothing is worse than no affordance. */}

        <div className="chat-input-area__field">
          <textarea
            className="chat-input-area__textarea"
            ref={textarea}
            value={value}
            onChange={e => setValue(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={placeholder}
            rows={1}
            disabled={disabled}
            aria-label="Message input"
          />
        </div>

        <button
          className={`btn btn--action btn--icon chat-input-area__send ${!value.trim() ? 'chat-input-area__send--disabled' : ''}`}
          onClick={handleSend}
          disabled={!value.trim() || disabled}
          aria-label="Send message"
        >
          <Send size={14} />
        </button>
      </div>

      {/* The one disclosure that has to be on screen wherever a question can be asked. Trimmed from
          two sentences to one clause and one claim: the earlier wording named the two companies
          before it got to the point, and this screen already carries the brand twice. */}
      <p className="chat-input-area__hint">
        Chat explains the recorded findings — <strong>deterministic rules decide every verdict</strong>.
      </p>
    </div>
  );
}
