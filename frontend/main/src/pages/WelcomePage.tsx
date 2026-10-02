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
 *
 * **The trailing disclosure was here twice.** This screen printed "GV Review uses deterministic
 * rules. AI extracts values; Python decides." directly under `ChatInput`, which prints its own
 * version of the same sentence — two lines of small grey text saying one thing, stacked. The line
 * belongs to the input, because it is true wherever the input appears; this screen no longer
 * repeats it.
 */

import { FilePlus2, ArrowUpRight } from 'lucide-react';
import { ChatInput } from '../components/chat/ChatInput';
import { GVMark } from '../components/brand/GVMark';
import { StatusBadge } from '../components/ui/Badge';
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
    <div className="welcome">
      <div className="welcome__inner">
        <header className="welcome__head">
          <GVMark size={44} className="welcome__mark" />
          <h1 className="welcome__greeting">{getGreeting()}</h1>
          <p className="welcome__subtitle">Which document set would you like to review?</p>
        </header>

        <div className="welcome__input">
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

        {/* Shaped like the list it becomes, so nothing jumps when the packages land. */}
        {packages.status === 'loading' && (
          <div className="welcome__recent" aria-label="Loading packages">
            {[0, 1, 2].map((i) => (
              <div key={i} className="welcome__card welcome__card--skeleton">
                <span className="skeleton" style={{ width: '46%', height: 13 }} />
                <span className="skeleton" style={{ width: '28%', height: 11 }} />
              </div>
            ))}
          </div>
        )}

        {/* Failure is said out loud. "No packages" on a screen that could not reach the server reads
            as an empty project, and the reviewer goes looking for work that is actually there. */}
        {packages.status === 'error' && (
          <p className="welcome__error" role="alert">
            Your packages could not be loaded — {packages.error.message}
          </p>
        )}

        {packages.status === 'ready' && recent.length === 0 && (
          <button className="welcome__empty interactive" onClick={onNewPackage}>
            <FilePlus2 size={16} />
            Submit the first document set
          </button>
        )}

        {recent.length > 0 && (
          <section className="welcome__recent stagger" aria-label="Recent document sets">
            {recent.map((pkg) => (
              <button
                key={pkg.id}
                className="welcome__card interactive"
                onClick={() => open(pkg.id)}
              >
                <span className="welcome__card-main">
                  <span className="welcome__card-vendor">{pkg.vendor ?? 'Package'}</span>
                  <span className="welcome__card-id mono">{pkg.id}</span>
                </span>
                <span className="welcome__card-right">
                  <StatusBadge status={pkg.state} />
                  <ArrowUpRight size={14} className="welcome__card-arrow" />
                </span>
              </button>
            ))}
          </section>
        )}
      </div>
    </div>
  );
}
