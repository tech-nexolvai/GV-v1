import { Menu } from 'lucide-react';
import { GVMark } from '../brand/GVMark';
import './Topbar.css';

interface TopbarProps {
  /** What this screen is — the review's vendor, or the page name. */
  title: string;
  /** Opens the sidebar drawer; only shown under 768px. */
  onOpenMenu: () => void;
  /** Where the current screen places its own actions (see `ShellHeader`). */
  actionsRef: (element: HTMLDivElement | null) => void;
  /** Where the current screen places a line beside its title (status, revision). */
  titleRef: (element: HTMLDivElement | null) => void;
}

/**
 * The header: the screen's title on the left, Graniti Vicentia in the centre, the screen's own
 * actions on the right — the client's brief, and the shape of every chat product they have used.
 */
export function Topbar({ title, onOpenMenu, actionsRef, titleRef }: TopbarProps) {
  return (
    <header className="topbar">
      <div className="topbar__left">
        <button
          type="button"
          className="topbar__menu"
          onClick={onOpenMenu}
          aria-label="Open menu"
        >
          <Menu size={20} />
        </button>
        <h1 className="topbar__title" title={title}>{title}</h1>
        <div className="topbar__title-extra" ref={titleRef} />
      </div>

      <div className="topbar__brand" aria-label="Graniti Vicentia">
        <GVMark size={24} />
        <span className="topbar__brand-name">Graniti Vicentia</span>
      </div>

      <div className="topbar__right" ref={actionsRef} />
    </header>
  );
}
