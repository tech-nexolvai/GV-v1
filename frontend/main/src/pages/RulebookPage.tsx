/**
 * The rules the engine would actually apply.
 *
 * This page used to render a hardcoded list, and it had drifted past "placeholder" into wrong: it
 * described every check as `within_tolerance` with a `± 3.175 mm` band, when V1 was decided as exact
 * match with no band at all, and it stated tolerances in millimetres, which are never a verdict
 * operand. A reviewer reading it would have come away believing the system works a way it does not.
 *
 * So it reads from `GET /api/v1/rules`, and shows only fields the API returns. Where the old list had
 * a formula, an applicability expression and a list of operands, this shows nothing — those are not
 * on the wire yet, and inventing them is how the previous version went wrong.
 */

import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import { listRules } from '../api/client';
import type { Rule } from '../api/client';
import { useAsync } from '../api/useAsync';
import './RulebookPage.css';

/** Derived from what the API returns, rather than a fixed list that could name a type with no rules. */
function categoriesOf(rules: readonly Rule[]): string[] {
  return ['all', ...[...new Set(rules.map((rule) => rule.product_type))].sort()];
}

export function RulebookPage() {
  const rules = useAsync(() => listRules(), []);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeCategory, setActiveCategory] = useState('all');

  if (rules.status === 'loading') {
    return (
      <div className="rulebook-page animate-fade-in">
        <div className="rulebook-detail__body">
          <p className="text-muted">Loading the rulebook…</p>
        </div>
      </div>
    );
  }

  // Failure and emptiness are different answers and must not look alike. "No rules" on a screen that
  // could not reach the server would read as *this system checks nothing*, which is a claim nobody
  // made.
  if (rules.status === 'error') {
    return (
      <div className="rulebook-page animate-fade-in">
        <div className="rulebook-detail__body" role="alert">
          <h2 className="rulebook-detail__name">The rulebook could not be loaded</h2>
          <div className="gv-bar" />
          <p>{rules.error.message}</p>
          <p className="text-muted">
            This is not the same as there being no rules — nothing is known either way from here.
          </p>
        </div>
      </div>
    );
  }

  const all = rules.data;
  const categories = categoriesOf(all);
  const filtered = all.filter((rule) => activeCategory === 'all' || rule.product_type === activeCategory);
  const selected = filtered.find((rule) => rule.rule_id === selectedId) ?? filtered[0];

  return (
    <div className="rulebook-page animate-fade-in">
      <div className="rulebook-sidebar">
        <div className="rulebook-sidebar__header">
          <div className="gv-bar" />
          <h1 className="rulebook-sidebar__title">Rulebook</h1>
          <p className="rulebook-sidebar__subtitle">
            {all.length === 0
              ? 'Nothing published'
              : `${all.length} rule${all.length === 1 ? '' : 's'} published`}
          </p>

          {categories.length > 1 && (
            <div className="rulebook-sidebar__filter">
              {categories.map((category) => (
                <button
                  key={category}
                  className={`rulebook-sidebar__filter-btn ${activeCategory === category ? 'rulebook-sidebar__filter-btn--active' : ''}`}
                  onClick={() => setActiveCategory(category)}
                >
                  {category === 'all' ? 'All' : category.charAt(0).toUpperCase() + category.slice(1)}
                </button>
              ))}
            </div>
          )}
        </div>

        <div className="rulebook-sidebar__list">
          {filtered.map((rule) => (
            <button
              key={rule.rule_id}
              className={`rulebook-rule-item ${selected?.rule_id === rule.rule_id ? 'rulebook-rule-item--active' : ''}`}
              onClick={() => setSelectedId(rule.rule_id)}
            >
              <div className="rulebook-rule-item__top">
                <span className="rulebook-rule-item__id">{rule.rule_id}</span>
                <span
                  className={`rulebook-rule-item__status rulebook-rule-item__status--${rule.production_ready ? 'published' : 'draft'}`}
                >
                  {rule.production_ready ? 'production' : 'not production'}
                </span>
              </div>
              <span className="rulebook-rule-item__name">{rule.name}</span>
              <div className="rulebook-rule-item__bottom">
                <span className="rulebook-rule-item__sev">{rule.severity}</span>
                <ChevronRight size={11} className="rulebook-rule-item__arrow" />
              </div>
            </button>
          ))}
        </div>
      </div>

      <div className="rulebook-detail">
        {selected === undefined ? (
          <div className="rulebook-detail__body">
            <h2 className="rulebook-detail__name">No rules are published</h2>
            <div className="gv-bar" />
            <p>
              Nothing has been published to the rulebook, so the engine has no checks to apply. This is
              the expected state before D6 publishes the first snapshot — it is not a fault, and it is
              not a filter hiding anything.
            </p>
            <div className="rulebook-detail__notice">
              <p>
                Rules are authored in YAML, validated with Pydantic and JSON Schema, and stored as
                immutable snapshots. Publishing needs human approval and a full gold-set regression.
              </p>
            </div>
          </div>
        ) : (
          <>
            <div className="rulebook-detail__header">
              <div>
                <div className="rulebook-detail__id-row">
                  <span className="rulebook-detail__id">{selected.rule_id}</span>
                  <span
                    className={`badge ${selected.severity === 'CRITICAL' ? 'badge--fail' : selected.severity === 'MAJOR' ? 'badge--review' : 'badge--missing'}`}
                  >
                    {selected.severity}
                  </span>
                  <span className={`badge ${selected.production_ready ? 'badge--pass' : 'badge--review'}`}>
                    {selected.production_ready ? 'production ready' : 'not production ready'}
                  </span>
                </div>
                <h2 className="rulebook-detail__name">{selected.name}</h2>
                <div className="gv-bar" />
              </div>
            </div>

            <div className="rulebook-detail__body">
              <RuleField label="Product type" value={selected.product_type} mono />
              <RuleField label="Check type" value={selected.check_type} mono />
              <RuleField label="Version" value={selected.version} mono />
              {/* The content hash of the exact bytes that were published. It is what a finding cites,
                  and comparing the two is how you tell which snapshot judged a drawing. */}
              <RuleField label="Snapshot" value={selected.snapshot_id} mono />
              <RuleField label="Published versions" value={String(selected.published_versions)} />
              {/* Warned on, not hidden. An unconfirmed tolerance is a number nobody has agreed to, and
                  it is the reason a rule can exist and still not be allowed near production. */}
              <RuleField
                label="Unconfirmed tolerances"
                value={
                  selected.unconfirmed_tolerances === 0
                    ? 'none'
                    : `${selected.unconfirmed_tolerances} — this rule cannot publish to production`
                }
                warning={selected.unconfirmed_tolerances > 0}
              />
              {selected.release_note.trim() !== '' && (
                <RuleField label="Release note" value={selected.release_note} />
              )}

              <div className="rulebook-detail__notice">
                <p>
                  Rules are authored in YAML, validated with Pydantic and JSON Schema, and stored as
                  immutable snapshots. Publishing needs human approval and a full gold-set regression.
                </p>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function RuleField({
  label,
  value,
  mono,
  warning,
}: {
  label: string;
  value: string;
  mono?: boolean;
  warning?: boolean;
}) {
  return (
    <div className="rulebook-detail__field">
      <span className="rulebook-detail__field-label">{label}</span>
      <span
        className={`rulebook-detail__field-value ${mono ? 'mono' : ''} ${warning ? 'rulebook-detail__field-value--warning' : ''}`}
      >
        {value}
      </span>
    </div>
  );
}
