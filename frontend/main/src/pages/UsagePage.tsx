import './UsagePage.css';

const STATS = [
  { label: 'Packages reviewed', value: '24', sub: 'this month' },
  { label: 'Checks run',        value: '312', sub: 'total' },
  { label: 'False-PASS rate',   value: '0.0%', sub: 'critical — target < 0.1%' },
  { label: 'Avg. review time',  value: '12 min', sub: 'per package' },
];

const OUTCOME_BREAKDOWN = [
  { label: 'PASS',     count: 198, pct: 63, cls: 'usage-bar--pass' },
  { label: 'FAIL',     count: 47,  pct: 15, cls: 'usage-bar--fail' },
  { label: 'REVIEW',   count: 42,  pct: 13, cls: 'usage-bar--review' },
  { label: 'NOT FOUND',count: 25,  pct: 8,  cls: 'usage-bar--missing' },
];

const ACTIVITY_LOG = [
  { actor: 'Raj Gupta',      action: 'Confirmed',  check: 'CT-2 Depth',      package: 'PKG-2026-001', time: '09:41' },
  { actor: 'Raj Gupta',      action: 'Exception',  check: 'CT-3a Sink Depth', package: 'PKG-2026-001', time: '09:38' },
  { actor: 'Raj Gupta',      action: 'Approved',   check: '— (full package)', package: 'PKG-2026-002', time: 'Aug 14, 16:20' },
  { actor: 'System',         action: 'Extracted',  check: '18 pages',         package: 'PKG-2026-003', time: '11:05' },
  { actor: 'Raj Gupta',      action: 'Corrected',  check: 'CAB-1 Width',      package: 'PKG-2026-004', time: 'Aug 12, 10:02' },
];

export function UsagePage() {
  return (
    <div className="usage-page animate-fade-in">
      <div className="usage-page__header">
        <div className="gv-bar" />
        <h1 className="usage-page__title">Usage</h1>
        <p className="usage-page__subtitle">Platform activity, outcome metrics, and audit log.</p>
      </div>

      <div className="usage-page__body">
        {/* Stat cards */}
        <div className="usage-stats">
          {STATS.map((s, i) => (
            <div key={i} className="usage-stat-card animate-slide-up" style={{ animationDelay: `${i * 60}ms` }}>
              <span className="usage-stat-card__value">{s.value}</span>
              <span className="usage-stat-card__label">{s.label}</span>
              <span className="usage-stat-card__sub">{s.sub}</span>
            </div>
          ))}
        </div>

        {/* Outcome breakdown */}
        <div className="usage-section">
          <h2 className="usage-section__title">Finding outcomes — all time</h2>
          <div className="usage-breakdown">
            {OUTCOME_BREAKDOWN.map((o, i) => (
              <div key={i} className="usage-breakdown__row animate-slide-up" style={{ animationDelay: `${i * 50}ms` }}>
                <span className="usage-breakdown__label">{o.label}</span>
                <div className="usage-breakdown__bar-wrap">
                  <div className={`usage-breakdown__bar ${o.cls}`} style={{ width: `${o.pct}%` }} />
                </div>
                <span className="usage-breakdown__pct">{o.pct}%</span>
                <span className="usage-breakdown__count">{o.count}</span>
              </div>
            ))}
          </div>
        </div>

        {/* Activity log */}
        <div className="usage-section">
          <h2 className="usage-section__title">Recent activity</h2>
          <div className="usage-log">
            {ACTIVITY_LOG.map((entry, i) => (
              <div key={i} className="usage-log__entry animate-fade-in" style={{ animationDelay: `${i * 40}ms` }}>
                <div className="usage-log__avatar">{entry.actor[0]}</div>
                <div className="usage-log__content">
                  <span className="usage-log__actor">{entry.actor}</span>
                  <span className="usage-log__action">{entry.action.toLowerCase()}</span>
                  <span className="usage-log__check">{entry.check}</span>
                  <span className="usage-log__sep">on</span>
                  <span className="usage-log__package">{entry.package}</span>
                </div>
                <span className="usage-log__time">{entry.time}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}
