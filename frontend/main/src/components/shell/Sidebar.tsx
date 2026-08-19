import { Package, BookOpen, BarChart2, MessageSquare, Plus, ChevronRight } from 'lucide-react';
import { MOCK_SESSIONS } from '../../data/mock';
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
  { id: 'packages',  label: 'Packages',  icon: Package },
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
  return (
    <aside
      className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''}`}
      aria-label="Navigation"
    >
      {/* Thread list — always visible */}
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
            {MOCK_SESSIONS.map(session => (
              <button
                key={session.id}
                className={`sidebar__thread ${activeSession === session.id ? 'sidebar__thread--active' : ''}`}
                onClick={() => onSelectSession?.(session.id)}
                aria-current={activeSession === session.id ? 'true' : undefined}
              >
                <div className="sidebar__thread-top">
                  <span className="sidebar__thread-id">
                    {session.package_label.split('-').map((part, i, arr) => (
                      <span key={i}>{part}{i < arr.length - 1 && <>{'-'}<wbr /></>}</span>
                    ))}
                  </span>
                  <StatusBadge status={session.status} size="sm" />
                </div>
                <span className="sidebar__thread-vendor truncate">{session.vendor}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {/* Main nav — always visible */}
      <nav className="sidebar__nav" aria-label="Primary navigation">
        {/* GV crimson bar separator */}
        {!collapsed && (
          <div className="sidebar__nav-divider">
            <div className="gv-bar gv-bar--full gv-bar--dim" />
          </div>
        )}

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
    </aside>
  );
}
