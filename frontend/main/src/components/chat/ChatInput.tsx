import { useState } from 'react';
import { Send, Paperclip } from 'lucide-react';
import './ChatInput.css';

interface ChatInputProps {
  onSend: (text: string) => void;
  disabled?: boolean;
  placeholder?: string;
}

const QUICK_PROMPTS = [
  'Run full review',
  'Show FAIL findings only',
  'Explain CT-1 result',
  'Generate vendor report',
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
        <button
          className="btn btn--subtle btn--icon"
          aria-label="Attach file"
          data-tooltip="Attach drawing"
          disabled={disabled}
        >
          <Paperclip size={15} />
        </button>

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
