import { useState } from 'react';
import { ChevronRight } from 'lucide-react';
import './RulebookPage.css';

const RULES = [
  {
    id: 'CT-1', name: 'Width Verification', category: 'countertop', severity: 'CRITICAL',
    operation: 'within_tolerance', status: 'published',
    description: 'Countertop overall width must equal the arch set wall-to-wall dimension plus two field-cut allowances.',
    inputs: ['shop_width (SHOP)', 'arch_width (ARCH)', 'field_cut_size (LITERAL)'],
    tolerance: '± 3.175 mm (1/8 in)',
    applicability: 'back_left_right (3-wall vanity)',
    snapshot: 'CT-v1.2 · 2026-08-10',
    formula: 'arch_width + (field_cut_size × 2)',
  },
  {
    id: 'CT-2', name: 'Depth Verification', category: 'countertop', severity: 'MAJOR',
    operation: 'within_tolerance', status: 'published',
    description: 'Countertop depth must match the cabinet depth plus the front overhang.',
    inputs: ['shop_depth (SHOP)', 'arch_depth (ARCH)'],
    tolerance: '± 3.175 mm (1/8 in)',
    applicability: 'global',
    snapshot: 'CT-v1.2 · 2026-08-10',
    formula: 'cabinet_depth + front_overhang',
  },
  {
    id: 'CT-3a', name: 'Sink Cutout Depth', category: 'countertop', severity: 'MAJOR',
    operation: 'within_tolerance', status: 'published',
    description: 'Sink cutout depth (D) as measured on the shop drawing must match the product spec.',
    inputs: ['shop_cutout_depth (SHOP)', 'spec_cutout_depth (PRODUCT_SPEC)'],
    tolerance: 'UNCONFIRMED — pending Q2 client answer',
    applicability: 'has_sink',
    snapshot: 'CT-v1.2 · 2026-08-10',
    formula: 'spec_cutout_depth',
  },
  {
    id: 'CAB-1', name: 'Cabinet Width Sum', category: 'cabinet', severity: 'MAJOR',
    operation: 'sum_within_tolerance', status: 'published',
    description: 'Sum of all cabinet widths plus fillers plus field cut must equal the arch wall-to-wall.',
    inputs: ['cab_widths[] (SHOP)', 'filler_l (SHOP)', 'filler_r (SHOP)', 'field_dimension (USER_INPUT)', 'arch_total (ARCH)'],
    tolerance: '± 3.175 mm (1/8 in)',
    applicability: 'back_left_right',
    snapshot: 'CT-v1.2 · 2026-08-10',
    formula: 'sum(cab_widths) + filler_l + filler_r + field_dimension',
  },
  {
    id: 'CAB-2', name: 'Filler Distribution', category: 'cabinet', severity: 'MINOR',
    operation: 'between', status: 'draft',
    description: 'Each filler panel must fall within the min/max range. Distribute excess across adjustable cabinets only.',
    inputs: ['filler_l (SHOP)', 'filler_r (SHOP)'],
    tolerance: 'min 25mm / max 75mm (Q2 OPEN)',
    applicability: 'back_left_right',
    snapshot: 'draft — not yet published',
    formula: 'min_filler ≤ filler_width ≤ max_filler',
  },
];

const CATEGORIES = ['all', 'countertop', 'cabinet'];

export function RulebookPage() {
  const [selectedRule, setSelectedRule] = useState(RULES[0]);
  const [activeCategory, setActiveCategory] = useState('all');

  const filtered = RULES.filter(r => activeCategory === 'all' || r.category === activeCategory);

  return (
    <div className="rulebook-page animate-fade-in">
      {/* Left: rule tree */}
      <div className="rulebook-sidebar">
        <div className="rulebook-sidebar__header">
          <div className="gv-bar" />
          <h1 className="rulebook-sidebar__title">Rulebook</h1>
          <p className="rulebook-sidebar__subtitle">Snapshot CT-v1.2</p>

          {/* Category filter */}
          <div className="rulebook-sidebar__filter">
            {CATEGORIES.map(cat => (
              <button
                key={cat}
                className={`rulebook-sidebar__filter-btn ${activeCategory === cat ? 'rulebook-sidebar__filter-btn--active' : ''}`}
                onClick={() => setActiveCategory(cat)}
              >
                {cat === 'all' ? 'All' : cat.charAt(0).toUpperCase() + cat.slice(1)}
              </button>
            ))}
          </div>
        </div>

        <div className="rulebook-sidebar__list">
          {filtered.map(rule => (
            <button
              key={rule.id}
              className={`rulebook-rule-item ${selectedRule.id === rule.id ? 'rulebook-rule-item--active' : ''}`}
              onClick={() => setSelectedRule(rule)}
            >
              <div className="rulebook-rule-item__top">
                <span className="rulebook-rule-item__id">{rule.id}</span>
                <span className={`rulebook-rule-item__status rulebook-rule-item__status--${rule.status}`}>
                  {rule.status}
                </span>
              </div>
              <span className="rulebook-rule-item__name">{rule.name}</span>
              <div className="rulebook-rule-item__bottom">
                <span className="rulebook-rule-item__sev rulebook-rule-item__sev--${rule.severity.toLowerCase()}">{rule.severity}</span>
                <ChevronRight size={11} className="rulebook-rule-item__arrow" />
              </div>
            </button>
          ))}
        </div>
      </div>

      {/* Right: rule detail */}
      <div className="rulebook-detail">
        <div className="rulebook-detail__header">
          <div>
            <div className="rulebook-detail__id-row">
              <span className="rulebook-detail__id">{selectedRule.id}</span>
              <span className={`badge ${selectedRule.severity === 'CRITICAL' ? 'badge--fail' : selectedRule.severity === 'MAJOR' ? 'badge--review' : 'badge--missing'}`}>
                {selectedRule.severity}
              </span>
              <span className={`badge ${selectedRule.status === 'published' ? 'badge--pass' : 'badge--review'}`}>
                {selectedRule.status}
              </span>
            </div>
            <h2 className="rulebook-detail__name">{selectedRule.name}</h2>
            <div className="gv-bar" />
          </div>
        </div>

        <div className="rulebook-detail__body">
          <RuleField label="Description" value={selectedRule.description} />
          <RuleField label="Operation" value={selectedRule.operation} mono />
          <RuleField label="Formula" value={selectedRule.formula} mono />
          <RuleField label="Tolerance" value={selectedRule.tolerance} warning={selectedRule.tolerance.includes('UNCONFIRMED')} />
          <RuleField label="Applicability" value={selectedRule.applicability} mono />
          <RuleField label="Rule snapshot" value={selectedRule.snapshot} mono />

          <div className="rulebook-detail__inputs">
            <span className="rulebook-detail__field-label">Inputs</span>
            <div className="rulebook-detail__input-list">
              {selectedRule.inputs.map((inp, i) => (
                <div key={i} className="rulebook-detail__input-item">
                  <code>{inp}</code>
                </div>
              ))}
            </div>
          </div>

          <div className="rulebook-detail__notice">
            <p>Rules are authored in YAML, validated with Pydantic + JSON Schema, and stored as immutable snapshots. Changes require human approval and a full gold-set regression before publication.</p>
          </div>
        </div>
      </div>
    </div>
  );
}

function RuleField({ label, value, mono, warning }: { label: string; value: string; mono?: boolean; warning?: boolean }) {
  return (
    <div className="rulebook-detail__field">
      <span className="rulebook-detail__field-label">{label}</span>
      <span className={`rulebook-detail__field-value ${mono ? 'mono' : ''} ${warning ? 'rulebook-detail__field-value--warning' : ''}`}>
        {value}
      </span>
    </div>
  );
}
