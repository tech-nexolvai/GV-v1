import { useId, useState } from 'react';
import type { MeasurementSection } from './measurementNavigation';

export function MeasurementSectionNav({ sections, onJump }: {
  sections: MeasurementSection[];
  onJump: (key: string) => void;
}) {
  const id = useId();
  const [selected, setSelected] = useState('overview');
  const destination = sections.some((section) => section.key === selected) ? selected : sections[0]?.key ?? '';
  return (
    <nav className="measurement-nav" aria-label="Measurement sections">
      <label htmlFor={id}>Jump to</label>
      <select id={id} value={destination} onChange={(event) => setSelected(event.target.value)}>
        {sections.map((section) => <option key={section.key} value={section.key}>{section.label}</option>)}
      </select>
      <button type="button" className="value-secondary" aria-label="Go to selected section"
        disabled={!destination} onClick={() => onJump(destination)}>Go</button>
    </nav>
  );
}
