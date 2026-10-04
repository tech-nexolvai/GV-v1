import { useEffect, useId, useRef, useState } from 'react';
import { ChevronDown } from 'lucide-react';

/** The same grounded question shortcuts at every width; only their presentation changes. */
export function PromptSuggestions({ prompts, disabled, onSend }: {
  prompts: readonly string[];
  disabled?: boolean;
  onSend: (question: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const listId = useId();
  const root = useRef<HTMLDivElement>(null);
  const toggle = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    function closeOutside(event: PointerEvent) {
      if (event.target instanceof Node && !root.current?.contains(event.target)) setOpen(false);
    }
    document.addEventListener('pointerdown', closeOutside);
    return () => document.removeEventListener('pointerdown', closeOutside);
  }, [open]);

  if (prompts.length === 0) return null;

  function close() {
    setOpen(false);
    if (toggle.current?.getClientRects().length) toggle.current.focus();
  }

  return (
    <div className="chat-input-area__quick" ref={root} data-open={open}
      onKeyDown={(event) => {
        if (event.key === 'Escape' && open) {
          event.preventDefault();
          event.stopPropagation();
          close();
        }
      }}
      onBlur={(event) => {
        if (event.relatedTarget instanceof Node && !event.currentTarget.contains(event.relatedTarget)) setOpen(false);
      }}>
      <div className="chat-input-area__quick-header">
        <span className="chat-input-area__quick-label">Suggested</span>
        <div className="chat-input-area__quick-sep" />
      </div>
      <button type="button" className="chat-input-area__quick-toggle" ref={toggle}
        aria-expanded={open} aria-controls={listId} onClick={() => setOpen(!open)}>
        Suggested questions <ChevronDown size={14} aria-hidden="true" />
      </button>
      <div className="chat-input-area__quick-list" id={listId}>
        {prompts.map(prompt => (
          <button type="button" key={prompt} className="chat-input-area__quick-btn" disabled={disabled}
            onClick={() => { if (!disabled) { close(); onSend(prompt); } }}>
            {prompt}
          </button>
        ))}
      </div>
    </div>
  );
}
