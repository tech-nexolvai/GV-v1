import { ChatInput } from '../components/chat/ChatInput';
import './WelcomePage.css';

interface WelcomePageProps {
  onStartSession: (sessionId: string) => void;
  onSend: (text: string) => void;
}

const WELCOME_CHIPS = [
  'Run full review on PKG-2026-001',
  'Show all FAIL findings',
  'Explain CT-1 width check',
  'Generate vendor report',
];

function getGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return 'Good morning';
  if (h < 17) return 'Good afternoon';
  return 'Good evening';
}

export function WelcomePage({ onStartSession, onSend }: WelcomePageProps) {
  function handleChip(prompt: string) {
    onStartSession('sess-001');
    setTimeout(() => onSend(prompt), 120);
  }

  function handleSend(text: string) {
    onStartSession('sess-001');
    setTimeout(() => onSend(text), 120);
  }

  return (
    <div className="welcome-page">
      <div className="welcome-page__inner">
        <h1 className="welcome-page__greeting">
          {getGreeting()}, <span className="welcome-page__name">Raj.</span>
        </h1>
        <p className="welcome-page__subtitle">
          What would you like to review today?
        </p>

        <div className="welcome-page__input-wrap">
          <ChatInput
            onSend={handleSend}
            placeholder="Ask about a document, run a review, explain a check…"
          />
        </div>

        <div className="welcome-page__chips">
          {WELCOME_CHIPS.map(chip => (
            <button
              key={chip}
              className="welcome-page__chip"
              onClick={() => handleChip(chip)}
            >
              {chip}
            </button>
          ))}
        </div>

        <p className="welcome-page__hint">
          GV Review uses deterministic rules. AI extracts values; Python decides.
        </p>
      </div>
    </div>
  );
}
