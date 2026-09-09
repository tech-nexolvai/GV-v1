import { PanelLeft } from 'lucide-react';
import './Topbar.css';

interface TopbarProps {
  onToggleSidebar: () => void;
  sidebarCollapsed: boolean;
}

export function Topbar({
  onToggleSidebar,
  sidebarCollapsed,
}: TopbarProps) {
  return (
    <header className="topbar" role="banner">
      <div className="topbar__left">
        <button
          className="btn btn--subtle btn--icon topbar__menu-btn"
          onClick={onToggleSidebar}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          data-tooltip={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <PanelLeft size={16} />
        </button>

        {/* GV Logo + wordmark */}
        <div className="topbar__brand">
          <div className="topbar__logo" aria-hidden="true">
            <span className="topbar__logo-v">GV</span>
          </div>
          <div className="topbar__wordmark">
            <span className="topbar__name">Graniti Vicentia</span>
            <span className="topbar__product">× Nexolv · Review Platform</span>
          </div>
        </div>
      </div>

      <div className="topbar__right" aria-label="Human-operated review workspace" />
    </header>
  );
}
