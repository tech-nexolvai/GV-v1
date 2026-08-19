import { useState, useEffect } from 'react';
import { Menu, Bell, Sun, Moon } from 'lucide-react';
import './Topbar.css';

interface TopbarProps {
  onToggleSidebar: () => void;
  sidebarCollapsed: boolean;
  designStyle: 'stone' | 'ide';
  onStyleChange: (style: 'stone' | 'ide') => void;
}

export function Topbar({
  onToggleSidebar,
  sidebarCollapsed,
  designStyle,
  onStyleChange,
}: TopbarProps) {
  const [theme, setTheme] = useState<'light' | 'dark'>(() => {
    const saved = localStorage.getItem('gv-theme');
    if (saved === 'dark') return 'dark';
    return 'light';
  });

  useEffect(() => {
    if (theme === 'dark') {
      document.documentElement.setAttribute('data-theme', 'dark');
    } else {
      document.documentElement.removeAttribute('data-theme');
    }
    localStorage.setItem('gv-theme', theme);
  }, [theme]);

  return (
    <header className="topbar" role="banner">
      <div className="topbar__left">
        <button
          className="btn btn--subtle btn--icon topbar__menu-btn"
          onClick={onToggleSidebar}
          aria-label={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          data-tooltip={sidebarCollapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          <Menu size={16} />
        </button>

        {/* GV Logo + wordmark */}
        <div className="topbar__brand">
          <div className="topbar__logo" aria-hidden="true">
            <span className="topbar__logo-v">GV</span>
          </div>
          <div className="topbar__wordmark">
            <span className="topbar__name">Graniti Vicentia</span>
            <span className="topbar__product">Review Platform</span>
          </div>
        </div>

        {/* Layout selection dropdown switcher */}
        <select
          className="topbar__style-select"
          value={designStyle}
          onChange={e => onStyleChange(e.target.value as any)}
          aria-label="Select layout style"
          data-tooltip="Change workspace layout"
        >
          <option value="stone">Standard Split</option>
          <option value="ide">IDE Assistant</option>
        </select>
      </div>

      <div className="topbar__right">
        <button
          className="btn btn--subtle btn--icon"
          onClick={() => setTheme(t => t === 'light' ? 'dark' : 'light')}
          aria-label={theme === 'light' ? 'Switch to Dark Mode' : 'Switch to Light Mode'}
          data-tooltip={theme === 'light' ? 'Dark Mode' : 'Light Mode'}
        >
          {theme === 'light' ? <Moon size={15} /> : <Sun size={15} />}
        </button>

        <button
          className="btn btn--subtle btn--icon"
          aria-label="Notifications"
          data-tooltip="Notifications"
        >
          <Bell size={15} />
        </button>

        <div className="topbar__user" aria-label="User menu">
          <div className="topbar__avatar" aria-hidden="true">R</div>
          <span className="topbar__username">Raj Gupta</span>
        </div>
      </div>
    </header>
  );
}

