import { useState } from 'react';
import { Send } from 'lucide-react';
import './ChatInput.css';

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

/**
 * The only three things this box can actually do.
 *
 * It used to suggest "Explain CT-1 result" and "Generate vendor report". There is no rule called
 * CT-1, nothing here explains a verdict, and no report is generated anywhere in this app — so two of
 * the four suggestions were instructions to ask for something that does not exist, and the reply
 * would have been the "not wired up" message. A suggestion chip is a promise about what the software
 * does; these are the ones it can keep.
 *
 * "Run full review" is gone for a narrower reason: it filters findings, it does not run anything.
 *
 * Each of these maps onto a branch of `filterFor` in `ReviewPage`. If that gains a filter, this gains
 * a chip — and if it loses one, a chip here starts falling through to "not wired up", which is
 * visible rather than silent.
 */
const QUICK_PROMPTS = [
  'Show all findings',
  'Show FAIL findings',
  'Show findings needing review',
];

export function ChatInput({ onSend, disabled, placeholder = 'Ask about this package…' }: ChatInputProps) {
  const [value, setValue] = useState('');

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

      <p className="chat-input-area__hint">
        GV Review uses deterministic rules. AI extracts values; Python decides.
      </p>
    </div>
  );
}
