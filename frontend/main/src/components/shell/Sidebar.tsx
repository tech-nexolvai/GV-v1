import { Files, BookOpen, BarChart2, MessageSquare, Plus, ChevronRight } from 'lucide-react';
import { listReviewSessions } from '../../api/client';
import { projectId } from '../../api/config';
import { useAsync } from '../../api/useAsync';
import { StatusBadge } from '../ui/Badge';
import './Sidebar.css';

interface SidebarProps {
  collapsed: boolean;
  activePage: string;
  activeSession?: string;
  onNavigate: (page: string) => void;
  onSelectSession?: (id: string) => void;
  onNewPackage?: () => void;
}

const NAV_ITEMS = [
  { id: 'review',    label: 'Review',    icon: MessageSquare },
  { id: 'documents', label: 'Documents', icon: Files },
  { id: 'rulebook',  label: 'Rulebook',  icon: BookOpen },
  { id: 'usage',     label: 'Usage',     icon: BarChart2 },
];

export function Sidebar({
  collapsed,
  activePage,
  activeSession,
  onNavigate,
  onSelectSession,
  onNewPackage,
}: SidebarProps) {
  // The reviewer's own open sittings. `mine` defaults to true on the server, which is what a
  // reviewer came back for — everyone's list would bury their own on a shared project.
  const sessions = useAsync(() => listReviewSessions(projectId()), []);
  const items = sessions.status === 'ready' ? sessions.data.items : [];

  return (
    <aside
      className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''}`}
      aria-label="Navigation"
    >
      {/* Main nav — top of sidebar */}
      <nav className="sidebar__nav" aria-label="Primary navigation">
        {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
          <button
            key={id}
            className={`sidebar__nav-item ${activePage === id ? 'sidebar__nav-item--active' : ''}`}
            onClick={() => onNavigate(id)}
            aria-label={label}
            data-tooltip={collapsed ? label : undefined}
            aria-current={activePage === id ? 'page' : undefined}
          >
            <Icon size={16} className="sidebar__nav-icon" />
            {!collapsed && <span className="sidebar__nav-label">{label}</span>}
            {!collapsed && activePage === id && (
              <ChevronRight size={12} className="sidebar__nav-chevron" />
            )}
          </button>
        ))}
      </nav>

      {/* Thread list — directly below nav, no gap */}
      {!collapsed && (
        <div className="sidebar__threads">
          <div className="sidebar__threads-header">
            <span className="sidebar__section-label">Reviews</span>
            <button
              className="btn btn--subtle btn--icon btn--sm"
              aria-label="New review"
              data-tooltip="New review"
              onClick={onNewPackage}
            >
              <Plus size={13} />
            </button>
          </div>

          <div className="sidebar__thread-list">
            {sessions.status === 'error' && (
              /* Named, not rendered as an empty list. An empty sidebar reads as "no reviews open",
                 which is a different and more comfortable statement than "we could not ask". */
              <p className="sidebar__thread-state sidebar__thread-state--error">
                Could not load reviews.
              </p>
            )}
            {sessions.status === 'ready' && items.length === 0 && (
              <p className="sidebar__thread-state">No open reviews.</p>
            )}
            {items.map(session => (
              <button
                key={session.id}
                className={`sidebar__thread ${activeSession === session.id ? 'sidebar__thread--active' : ''}`}
                onClick={() => onSelectSession?.(session.id)}
                aria-current={activeSession === session.id ? 'true' : undefined}
              >
                <div className="sidebar__thread-top">
                  <span className="sidebar__thread-id">
                    {/* The revision id until the API carries a package label. A real identifier a
                        reviewer can quote beats a friendly name that is in no record. */}
                    {session.package_revision_id.slice(0, 8)}
                  </span>
                  <StatusBadge
                    status={session.completed_at ? 'APPROVED' : 'AWAITING_REVIEW'}
                    size="sm"
                  />
                </div>
                <span className="sidebar__thread-vendor truncate">{session.reviewer}</span>
              </button>
            ))}
          </div>
        </div>
      )}
      {/* Deliberately unnamed. This said "Raj Gupta" for everyone who opened it — the client's name,
          shown as though it were the signed-in reviewer, next to actions somebody else performed.
          There is no endpoint that reports who you are, so the honest label is a generic one, and it
          becomes a real name when identity is on the wire rather than before. */}
      <div className="sidebar__footer">
        <div className="sidebar__user" aria-label="User menu">
          <div className="sidebar__avatar" aria-hidden="true">·</div>
          {!collapsed && <span className="sidebar__username">Signed in</span>}
        </div>
      </div>
    </aside>
  );
}
