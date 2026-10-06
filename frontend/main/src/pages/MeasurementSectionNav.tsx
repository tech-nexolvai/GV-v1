const steps = [
  { target: 'measure-drawings', number: '01', label: 'Identify drawings and parts' },
  { target: 'measure-runs', number: '02', label: 'Confirm runs and widths' },
  { target: 'measure-values', number: '03', label: 'Review measurements' },
  { target: 'measure-settings', number: '04', label: 'Set requirements and run checks' },
] as const;

/** The app routes through `#`; scroll buttons must never replace the package route. */
export function MeasurementSectionNav() {
  return (
    <nav className="measure-steps" aria-label="Measurement steps">
      {steps.map((step) => (
        <button type="button" aria-controls={step.target} key={step.target} onClick={() => {
          document.getElementById(step.target)?.scrollIntoView({ behavior: 'auto', block: 'start' });
        }}>
          <span aria-hidden="true">{step.number}</span>
          {step.label}
        </button>
      ))}
    </nav>
  );
}
