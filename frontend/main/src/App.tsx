import { useState, useEffect } from 'react';
import { AppShell } from './components/shell/AppShell';
import { ReviewPage } from './pages/ReviewPage';
import { PackagesPage } from './pages/PackagesPage';
import { WelcomePage } from './pages/WelcomePage';
import { RulebookPage } from './pages/RulebookPage';
import { UsagePage } from './pages/UsagePage';
import { MOCK_SESSIONS, MOCK_PACKAGES_LIST } from './data/mock';
import { X, UploadCloud, Loader2, CheckCircle2 } from 'lucide-react';
import './design/components.css';

const SIMULATION_STEPS = [
  { label: 'Uploading drawings to S3 vault...', meta: 'Generating SHA-256...' },
  { label: 'Registering metadata in PostgreSQL...', meta: 'Triggering Hatchet outbox event...' },
  { label: 'Running class check & PaddleOCR layout analysis...', meta: 'PaddleOCR + docTR active...' },
  { label: 'Extracting digital vector lines with pdfplumber...', meta: 'Parallel lanes running...' },
  { label: 'Normalizing shapes & loading rulebook snapshot...', meta: 'Resolving CT-v1.2 active rules...' },
  { label: 'Evaluating operands inside isolated Verdict engine...', meta: 'Exact Fraction math sealed...' }
];

export default function App() {
  const [activePage, setActivePage] = useState<string>('review');
  const [activeSession, setActiveSession] = useState<string>('');
  const [evidencePanel, setEvidencePanel] = useState<React.ReactNode>(null);
  const [pendingMessage, setPendingMessage] = useState<string>('');

  const [designStyle, setDesignStyle] = useState<'stone' | 'ide'>(() => {
    const saved = localStorage.getItem('gv-design-style');
    if (saved === 'ide') return 'ide';
    return 'stone';
  });

  useEffect(() => {
    document.documentElement.setAttribute('data-style', designStyle);
    localStorage.setItem('gv-design-style', designStyle);
  }, [designStyle]);

  // New package form states
  const [isModalOpen, setIsModalOpen] = useState(false);
  const [vendor, setVendor] = useState('Apex Glass & Stone');
  const [project, setProject] = useState('Westin Towers — Double Countertops');
  const [category, setCategory] = useState<'countertop' | 'cabinet'>('countertop');
  const [rulebook, setRulebook] = useState('CT-v1.2');
  const [archFileName, setArchFileName] = useState('');
  const [shopFileName, setShopFileName] = useState('');

  // Simulation steps states
  const [isSimulating, setIsSimulating] = useState(false);
  const [simulationStep, setSimulationStep] = useState(0);

  function handleNavigate(page: string) {
    setActivePage(page);
    setEvidencePanel(null);
  }

  function handleSelectSession(id: string) {
    setActiveSession(id);
    setActivePage('review');
    setEvidencePanel(null);
  }

  function handleOpenReview(packageId: string) {
    const session = MOCK_SESSIONS.find(s => s.package_id === packageId) ?? MOCK_SESSIONS[0];
    setActiveSession(session.id);
    setActivePage('review');
    setEvidencePanel(null);
  }

  /** Called from WelcomePage — picks a session then queues the message */
  function handleWelcomeStart(sessionId: string) {
    setActiveSession(sessionId);
    setActivePage('review');
    setEvidencePanel(null);
  }

  function handleWelcomeSend(text: string) {
    setPendingMessage(text);
  }

  function triggerSubmitPipeline() {
    setIsSimulating(true);
    setSimulationStep(0);

    const runStep = (stepIndex: number) => {
      if (stepIndex < SIMULATION_STEPS.length) {
        setSimulationStep(stepIndex);
        setTimeout(() => {
          runStep(stepIndex + 1);
        }, 1100);
      } else {
        const nextIndex = MOCK_SESSIONS.length + 1;
        const newPkgId = `PKG-2026-00${nextIndex}`;
        const newSessId = `sess-00${nextIndex}`;

        // Create mock findings for the new package session
        const newFindings: any[] = [
          {
            id: `f${nextIndex}-001`,
            check_id: 'CT-1',
            name: 'Width Verification',
            outcome: 'PASS',
            severity: 'CRITICAL',
            expected: '5420 mm',
            found: '5420 mm',
            delta: '0 mm',
            tolerance: '± 3.175 mm',
            reason: null,
            trace: {
              operation: 'within_tolerance',
              operands: [
                { name: 'shop_width', value: '5420 mm', source: 'SHOP' },
                { name: 'arch_width', value: '5420 mm', source: 'ARCH' },
                { name: 'tolerance', value: '3.175 mm', source: 'LITERAL' },
              ],
              comparison: '|5420 - 5420| = 0 mm ≤ 3.175 mm',
              tolerance: '3.175 mm',
              arithmetic_unit: 'mm',
              outcome: 'PASS',
            },
            arch_evidence: { role: 'ARCH', page: 1, polygon: [[100, 100], [200, 100], [200, 120], [100, 120]], raw_text: '5420 [213 3/8]', extractor: 'pdfplumber' },
            shop_evidence: { role: 'SHOP', page: 2, polygon: [[110, 110], [210, 110], [210, 130], [110, 130]], raw_text: '5420 [213 3/8]', extractor: 'pdfplumber' },
            reviewer_action: null,
            reviewer_note: null,
          },
          {
            id: `f${nextIndex}-002`,
            check_id: 'CT-3a',
            name: 'Sink Cutout Depth',
            outcome: 'REVIEW_REQUIRED',
            severity: 'MAJOR',
            expected: null,
            found: null,
            delta: null,
            tolerance: null,
            reason: 'OCR mismatch: PaddleOCR read 324mm while docTR read 328mm on page 4. Reviewer must resolve site VIF or verify dimensions.',
            trace: null,
            arch_evidence: { role: 'ARCH', page: 2, polygon: [[200, 200], [300, 200], [300, 220], [200, 220]], raw_text: '325 [12 13/16]', extractor: 'pdfplumber' },
            shop_evidence: { role: 'SHOP', page: 4, polygon: [[210, 210], [310, 210], [310, 230], [210, 230]], raw_text: '324 [12 3/4] / 328 [12 29/32]', extractor: 'paddleocr+doctr' },
            reviewer_action: null,
            reviewer_note: null,
          }
        ];

        // Create new Review Session
        const newSession = {
          id: newSessId,
          package_id: newPkgId,
          package_label: newPkgId,
          vendor: vendor,
          status: 'AWAITING_REVIEW' as any,
          last_activity: new Date().toISOString(),
          messages: [
            {
              id: `msg-${nextIndex}-001`,
              role: 'user' as const,
              content: `Validate new drawings for ${project}. Category: ${category}.`,
              timestamp: new Date(Date.now() - 10000).toISOString(),
            },
            {
              id: `msg-${nextIndex}-002`,
              role: 'assistant' as const,
              content: `Ingestion & parallel extraction complete for **${newPkgId}**.\n\nS3 object paths:\n- Architectural: \`s3://gv-vault/${newPkgId}/arch.pdf\` (sha256: \`e97a...5a23\`)\n- Shop Drawing: \`s3://gv-vault/${newPkgId}/shop.pdf\` (sha256: \`b4f8...10de\`)\n\nRunning rule engine evaluation against snapshot **${rulebook}**. 1 PASS check and 1 REVIEW check resolved. Detailed checklist is displayed below:`,
              timestamp: new Date().toISOString(),
              findings: newFindings,
            }
          ]
        };

        // Create new Package List entry
        const newPkg = {
          id: newPkgId,
          vendor: vendor,
          project: project,
          category: category,
          status: 'AWAITING_REVIEW' as any,
          submitted_at: new Date().toISOString(),
          reviewer: 'Raj Gupta',
          pass_count: 1,
          fail_count: 0,
          review_count: 1,
          missing_count: 0,
        };

        MOCK_SESSIONS.push(newSession);
        MOCK_PACKAGES_LIST.push(newPkg);

        setIsSimulating(false);
        setIsModalOpen(false);

        // Auto-navigate to the new session
        setActiveSession(newSessId);
        setActivePage('review');
        setEvidencePanel(null);

        // Reset file selections
        setArchFileName('');
        setShopFileName('');
      }
    };

    runStep(0);
  }

  return (
    <AppShell
      activePage={activePage}
      onNavigate={handleNavigate}
      activeSession={activeSession}
      onSelectSession={handleSelectSession}
      evidencePanel={evidencePanel}
      onNewPackage={() => setIsModalOpen(true)}
      designStyle={designStyle}
      onStyleChange={setDesignStyle}
    >
      {activePage === 'review' && !activeSession && (
        <WelcomePage
          onStartSession={handleWelcomeStart}
          onSend={handleWelcomeSend}
        />
      )}

      {activePage === 'review' && activeSession && (
        <ReviewPage
          key={activeSession}
          sessionId={activeSession}
          onEvidenceChange={setEvidencePanel}
          initialMessage={pendingMessage}
          onMessageConsumed={() => setPendingMessage('')}
        />
      )}

      {activePage === 'documents' && (
        <PackagesPage
          onOpenReview={handleOpenReview}
          onNewPackage={() => setIsModalOpen(true)}
        />
      )}

      {activePage === 'rulebook' && <RulebookPage />}

      {activePage === 'usage' && <UsagePage />}

      {/* ── NEW PACKAGE UPLOAD MODAL ───────────────────────── */}
      {isModalOpen && (
        <div className="modal-overlay" role="dialog" aria-modal="true">
          <div className="modal">
            <div className="modal__header">
              <span className="modal__title">
                {isSimulating ? 'Processing Ingestion Pipeline' : 'Submit New Document'}
              </span>
              {!isSimulating && (
                <button
                  className="btn btn--subtle btn--icon btn--sm"
                  onClick={() => setIsModalOpen(false)}
                  aria-label="Close modal"
                >
                  <X size={15} />
                </button>
              )}
            </div>

            {isSimulating ? (
              /* Simulated Pipeline status screen */
              <div className="modal__body pipeline-overlay">
                <Loader2 className="pipeline-loader" size={32} />
                <div className="pipeline-steps">
                  {SIMULATION_STEPS.map((step, idx) => {
                    const isCompleted = idx < simulationStep;
                    const isActive = idx === simulationStep;
                    return (
                      <div
                        key={idx}
                        className={`pipeline-step ${isCompleted ? 'pipeline-step--completed' : ''} ${isActive ? 'pipeline-step--active' : ''}`}
                      >
                        <div className="pipeline-step__status">
                          {isCompleted ? (
                            <CheckCircle2 size={15} />
                          ) : isActive ? (
                            <Loader2 size={13} className="animate-spin" />
                          ) : (
                            <div style={{ width: 13, height: 13, border: '1px solid var(--border-default)', borderRadius: '50%' }} />
                          )}
                        </div>
                        <span className="pipeline-step__label">{step.label}</span>
                        <span className="pipeline-step__meta">{isCompleted ? 'Done' : isActive ? step.meta : 'Waiting'}</span>
                      </div>
                    );
                  })}
                </div>
              </div>
            ) : (
              /* Package Submission Form */
              <>
                <div className="modal__body">
                  <div className="form-row">
                    <div className="form-group">
                      <label className="form-group__label">Vendor Name</label>
                      <input
                        className="input"
                        value={vendor}
                        onChange={e => setVendor(e.target.value)}
                        placeholder="e.g. Apex Glass & Stone"
                      />
                    </div>
                    <div className="form-group">
                      <label className="form-group__label">Rulebook snapshot</label>
                      <select
                        className="input"
                        value={rulebook}
                        onChange={e => setRulebook(e.target.value)}
                      >
                        <option value="CT-v1.2">Countertop CT-v1.2</option>
                        <option value="CAB-v1.0">Cabinet CAB-v1.0</option>
                      </select>
                    </div>
                  </div>

                  <div className="form-group">
                    <label className="form-group__label">Project Context</label>
                    <input
                      className="input"
                      value={project}
                      onChange={e => setProject(e.target.value)}
                      placeholder="e.g. Westin Towers — Vanity Counters"
                    />
                  </div>

                  <div className="form-row">
                    <div className="form-group">
                      <label className="form-group__label">Category Scope</label>
                      <select
                        className="input"
                        value={category}
                        onChange={e => setCategory(e.target.value as any)}
                      >
                        <option value="countertop">Countertop</option>
                        <option value="cabinet">Cabinet</option>
                      </select>
                    </div>
                    <div className="form-group">
                      <label className="form-group__label">Upload Target Environment</label>
                      <input
                        className="input"
                        disabled
                        value="Immutable S3 (gv-vault)"
                      />
                    </div>
                  </div>

                  <div className="form-group">
                    <label className="form-group__label">Upload Drawings (PDF only)</label>
                    <div className="upload-zone-wrapper">
                      {/* Arch upload zone */}
                      <div
                        className={`upload-zone ${archFileName ? 'upload-zone--has-file' : ''}`}
                        onClick={() => setArchFileName('GV_Arch_Westin_R2.pdf')}
                        role="button"
                        aria-label="Upload Architectural Drawing Set"
                      >
                        <UploadCloud size={20} className="text-muted" />
                        <span className="upload-zone__title">Architectural Set</span>
                        {archFileName ? (
                          <span className="upload-zone__filename">{archFileName}</span>
                        ) : (
                          <span className="upload-zone__desc">Click to select PDF</span>
                        )}
                      </div>

                      {/* Shop drawing upload zone */}
                      <div
                        className={`upload-zone ${shopFileName ? 'upload-zone--has-file' : ''}`}
                        onClick={() => setShopFileName('ApexStone_Shop_V1.pdf')}
                        role="button"
                        aria-label="Upload Shop Drawing Set"
                      >
                        <UploadCloud size={20} className="text-muted" />
                        <span className="upload-zone__title">Shop Drawings</span>
                        {shopFileName ? (
                          <span className="upload-zone__filename">{shopFileName}</span>
                        ) : (
                          <span className="upload-zone__desc">Click to select PDF</span>
                        )}
                      </div>
                    </div>
                  </div>
                </div>

                <div className="modal__footer">
                  <button
                    className="btn btn--subtle"
                    onClick={() => setIsModalOpen(false)}
                  >
                    Cancel
                  </button>
                  <button
                    className="btn btn--action"
                    disabled={!vendor || !project || !archFileName || !shopFileName}
                    onClick={triggerSubmitPipeline}
                  >
                    Run Review Pipeline
                  </button>
                </div>
              </>
            )}
          </div>
        </div>
      )}
    </AppShell>
  );
}
