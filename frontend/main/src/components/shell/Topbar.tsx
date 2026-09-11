import { PanelLeft } from 'lucide-react';
import { GVMark } from '../brand/GVMark';
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
          className="btn btn--subtle btn--icon topbar__menu-btn interactive"
          onClick={onToggleSidebar}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          data-tooltip={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <PanelLeft size={16} className="topbar__menu-icon" data-collapsed={sidebarCollapsed} />
        </button>

        <div className="topbar__brand">
          <GVMark size={28} />
          <div className="topbar__wordmark">
            <span className="topbar__name">Graniti Vicentia</span>
            <span className="topbar__product">Review Platform</span>
          </div>
        </div>
      </div>

      {/* The build partner, set apart from the product name rather than run into it. It was
          "× Nexolv · Review Platform" on one line, which read as a single four-part product name. */}
      <div className="topbar__right">
        <span className="topbar__partner">
          built with <strong>Nexolv</strong>
        </span>
      </div>
    </header>
  );
}
