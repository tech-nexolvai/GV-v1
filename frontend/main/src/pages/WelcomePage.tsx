/**
 * The landing screen, and the thing that decides which package a question is about.
 *
 * It used to call `onStartSession('sess-001')` — a package id that exists in no database. Every chip
 * and every typed message opened a review of nothing, which is the single reason this app looked
 * like it was running on fixtures: the first click always went somewhere the backend had never
 * heard of. The suggestion chips named `PKG-2026-001` and `CT-1` for the same reason.
 *
 * So the chips are gone and the list is real. A question needs a package to be about, and the only
 * packages that exist are the ones the API returns.
 */

import { ChatInput } from '../components/chat/ChatInput';
import { listPackages } from '../api/client';
import { projectId } from '../api/config';
import { useAsync } from '../api/useAsync';
import './WelcomePage.css';

interface WelcomePageProps {
  onStartSession: (packageId: string) => void;
  onSend: (text: string) => void;
  onNewPackage?: () => void;
}

function getGreeting(): string {
  const h = new Date().getHours();
  if (h < 12) return 'Good morning';
  if (h < 17) return 'Good afternoon';
  return 'Good evening';
}

/** The most recent packages, which is what somebody returning to this screen is looking for. */
const RECENT_LIMIT = 5;

export function WelcomePage({ onStartSession, onSend, onNewPackage }: WelcomePageProps) {
  const packages = useAsync(() => listPackages(projectId(), { limit: RECENT_LIMIT }), []);
  const recent = packages.status === 'ready' ? packages.data.items : [];

  function open(packageId: string, prompt?: string) {
    onStartSession(packageId);
    if (prompt !== undefined) onSend(prompt);
  }

  return (
    <div className="welcome-page">
      <div className="welcome-page__inner">
        <h1 className="welcome-page__greeting">{getGreeting()}.</h1>
        <p className="welcome-page__subtitle">What would you like to review today?</p>

        <div className="welcome-page__input-wrap">
          <ChatInput
            onSend={(text) => {
              // A question has to be about something. Sending it with no package selected used to
              // open a review of a package id that did not exist; now the most recent one is the
              // subject, and if there are none there is nothing to ask about yet.
              if (recent[0] !== undefined) open(recent[0].id, text);
            }}
            // Disabled rather than accepting and discarding. `ChatInput` clears the box on submit, so
            // a question typed before the packages arrived vanished with no message and no reply —
            // indistinguishable, from the outside, from the app having ignored it.
            disabled={packages.status !== 'ready' || recent.length === 0}
            placeholder={
              recent.length === 0
                ? 'Submit a document set first — there is nothing to review yet'
                : `Ask about ${recent[0]?.vendor ?? 'the latest package'}…`
            }
          />
        </div>

        {packages.status === 'loading' && <p className="welcome-page__hint">Loading your packages…</p>}

        {/* Failure is said out loud. "No packages" on a screen that could not reach the server reads
            as an empty project, and the reviewer goes looking for work that is actually there. */}
        {packages.status === 'error' && (
          <p className="welcome-page__hint" role="alert">
            Your packages could not be loaded — {packages.error.message}
          </p>
        )}

        {packages.status === 'ready' && recent.length === 0 && (
          <div className="welcome-page__chips">
            <button className="welcome-page__chip" onClick={onNewPackage}>
              Submit the first document set
            </button>
          </div>
        )}

        {recent.length > 0 && (
          <div className="welcome-page__chips">
            {recent.map((pkg) => (
              <button key={pkg.id} className="welcome-page__chip" onClick={() => open(pkg.id)}>
                {pkg.vendor ?? 'Package'} · {pkg.state.toLowerCase()}
              </button>
            ))}
          </div>
        )}

        <p className="welcome-page__hint">
          GV Review uses deterministic rules. AI extracts values; Python decides.
        </p>
      </div>
    </div>
  );
}
