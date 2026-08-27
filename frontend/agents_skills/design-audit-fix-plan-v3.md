# Design Audit Fix — Full Implementation Plan (v3)
*Covers all 12 + 7 detailed points from audit.md, plus review fixes from v2*

---

## Overview: Files Being Changed

| File | What Changes |
|---|---|
| `tokens.css` | Add `--color-primary` (blue, contrast-verified) + type/spacing scale audit + label contrast token |
| `components.css` | New `btn--action`, `btn--reviewer` variants with full interaction states; `.reviewer-actions` container treatment |
| `ReviewPage.tsx` | CTA color fix, header hierarchy, progress dots |
| `ReviewPage.css` | Vendor = H1, pkg-id = caption, progress fill = blue |
| `FindingCard.css` | Value sizes 1.5–2×, FAIL/PASS row differentiation (color + icon), reason line-height, card elevation on **both** summary and per-check cards |
| `FindingCard.tsx` | Action button class swap, `HelpCircle → MinusCircle`, mono on all mm values, delta icon pairing |
| `ChatInput.tsx` | "Suggested" label above chips |
| `ChatInput.css` | Placeholder contrast, send icon = blue, chip label styles |
| `Sidebar.css` | ID wrap at hyphen boundaries only, vendor truncate, slightly wider |
| `Badge.tsx` | NOT_FOUND dot glyph swap |
| *(new)* `audit.md` checklist | Add accessibility verification pass |
| *(new)* global sweep | Find/replace other non-destructive uses of `btn--primary` (crimson) app-wide |

---

## Proposed Changes

---

### LAYER 0 — Pre-Work: Verify Before Building

**Do these two checks before writing any CSS**, since they gate whether the rest of the plan is correct:

1. **Confirm the type/spacing scale actually exists as a consistent system.** Check `tokens.css` for `--text-xs/sm/md/lg/xl` and `--space-1` through `--space-5`. If these are ad hoc/inconsistent (e.g. not on a clean ratio), define them properly now:
   ```css
   /* Type scale */
   --text-xs:  12px;
   --text-sm:  14px;
   --text-md:  16px;
   --text-lg:  20px;
   --text-xl:  28px;

   /* Spacing scale */
   --space-1: 4px;
   --space-2: 8px;
   --space-3: 16px;
   --space-4: 24px;
   --space-5: 32px;
   ```
   Every size/spacing value touched below should map onto this scale — not introduce new one-off values.

2. **Grep the codebase for all other usages of `btn--primary`** (crimson) to find any other non-destructive CTA (Save, Confirm, Submit, etc.) that should also move to `btn--action`. Fixing Sign Off alone but leaving other primary actions red re-introduces the same red=action / red=error conflict elsewhere in the app.
   ```bash
   grep -rn "btn--primary" src/
   ```
   List every match and classify: destructive (keep red) vs. primary-but-not-destructive (swap to `btn--action`).

3. **Sanity-check the mascot icon in an incognito window** with no extensions before ruling it out as unrelated — confirm it's not injected by a shared dev overlay that other reviewers/testers would also see.

---

### LAYER 1 — Design Tokens

#### [MODIFY] `tokens.css`

**Add primary action color (blue) — audit point #2 "Sign Off uses danger color":**
```css
/* ── Primary action color — NEVER red ───────────────────── */
--color-primary:        #0f7dc1;
--color-primary-hover:  #0d6aa8;
--color-primary-dim:    rgba(15, 125, 193, 0.10);
--color-primary-border: rgba(15, 125, 193, 0.25);
--shadow-primary: 0 0 0 1px rgba(15, 125, 193, 0.2), 0 4px 12px rgba(15, 125, 193, 0.12);
```

**Contrast requirement — verify before merging:**
- `#0f7dc1` with white (`#fff`) text must meet **4.5:1** (normal text) — run through a contrast checker (e.g. WebAIM) in both light and dark surface contexts.
- `--color-primary-hover` (`#0d6aa8`) must also pass — check separately, hover states are often overlooked.
- If either fails, darken the base blue rather than relying on the hover state to compensate.

**Audit point #5 "low contrast label text" — raise label text to WCAG AA:**
```css
/* Light mode */
--text-muted: #6b5f58;   /* was #8c7e75 — now ≥4.5:1 against white, verify */
```
Verify in dark mode too — the muted token likely needs a separate value there, not just the light-mode fix.

**Audit point #12 "two type systems not applied consistently":**
- No new token; ensure `--font-mono` is applied consistently to every mm/numeric value span (see Layer 4 verification note).

---

### LAYER 2 — Component Base Styles

#### [MODIFY] `components.css`

**Audit #2 — `btn--action` (blue primary CTA):**
```css
.btn--action { background: var(--color-primary); color: #fff; border-color: var(--color-primary); }
.btn--action:hover { background: var(--color-primary-hover); box-shadow: var(--shadow-primary); }
.btn--action:focus-visible { outline: none; box-shadow: 0 0 0 3px var(--color-primary-dim); }
.btn--action:active { transform: translateY(1px); }
.btn--action:disabled { background: var(--bg-disabled); color: var(--text-muted); cursor: not-allowed; box-shadow: none; }
```

**Audit #3, #5 — `btn--reviewer` (visible ghost chrome, full state coverage):**
```css
.btn--reviewer {
  background: transparent;
  color: var(--text-body);
  border: 1px solid var(--border-strong);
  border-radius: var(--radius-sm);
  padding: 3px var(--space-3);
  font-size: var(--text-xs);
  font-weight: var(--weight-medium);
  transition: all var(--duration-fast) var(--ease-out);
}
.btn--reviewer:hover {
  background: var(--bg-hover);
  border-color: var(--border-strong);
  color: var(--text-primary);
}
.btn--reviewer:focus-visible {
  outline: none;
  box-shadow: 0 0 0 3px var(--color-primary-dim);
  border-color: var(--color-primary-border);
}
.btn--reviewer:active { background: var(--bg-pressed); }
.btn--reviewer:disabled { opacity: 0.5; cursor: not-allowed; }
.btn--reviewer--dismiss { color: var(--status-missing); }
.btn--reviewer--dismiss:hover { border-color: var(--status-missing); }
```

**New — reviewer actions container (groups the 4 buttons as one visual unit):**
```css
.finding-card__reviewer-actions {
  display: flex;
  gap: var(--space-1);
  flex-wrap: wrap;
  padding: var(--space-1);
  background: var(--bg-surface);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  width: fit-content;
}
```
This gives Confirm/Correct/Exception/Dismiss a shared container so they read as one decision toolbar rather than four unrelated buttons floating near each other.

---

### LAYER 3 — ReviewPage Header

#### [MODIFY] `ReviewPage.tsx`

- Change `btn--primary` → `btn--action` on Sign Off button.
- Restructure header hierarchy:
  - Move `pkg.vendor` above `pkg.id`; make it the visual H1.
  - `pkg.id` → demoted to mono caption, rendered below vendor.
  - `pkg.project` → subtitle, smaller/muted.

#### [MODIFY] `ReviewPage.css`

**Typography hierarchy:**
```css
.review-page__pkg-vendor  { font-size: var(--text-lg); font-weight: var(--weight-semi); color: var(--text-primary); }
.review-page__pkg-id      { font-size: var(--text-xs); font-weight: var(--weight-regular); color: var(--text-muted); font-family: var(--font-mono); }
.review-page__pkg-project { font-size: var(--text-xs); color: var(--text-muted); }
```

**Progress fill → blue not crimson:**
```css
.review-page__progress-fill { background: var(--color-primary); }
```

**Progress text quieter than CTA:**
```css
.review-page__progress-text { font-size: 11px; color: var(--text-muted); }
```

---

### LAYER 4 — FindingCard (biggest section)

#### [MODIFY] `FindingCard.css`

**Value size contrast (1.5–2× label):**
```css
.finding-card__value-label { font-size: 10px; color: var(--text-body); } /* contrast fix, not --text-muted */
.finding-card__value-number { font-size: var(--text-xl); font-weight: var(--weight-semi); font-family: var(--font-mono); }
.finding-card__value-number--delta { font-size: var(--text-md); }
```

**Status row differentiation (FAIL interrupts, PASS recedes):**
```css
.finding-card--fail { border-left: 3px solid var(--status-fail); }
.finding-card--fail .finding-card__name { font-weight: var(--weight-semi); color: var(--text-primary); }

.finding-card--review-required { border-left: 3px solid var(--status-review); }
.finding-card--review-required .finding-card__name { font-weight: var(--weight-medium); }

.finding-card--pass .finding-card__name { color: var(--text-muted); font-weight: var(--weight-regular); }
.finding-card--pass .finding-card__outcome-icon { opacity: 0.5; }
```

**Reason text line-height and breathing room:**
```css
.finding-card__reason {
  line-height: var(--leading-loose);
  margin: var(--space-3) 0;
  padding: var(--space-2) var(--space-3);
}
```

**Card elevation / surface separation — applied to BOTH the per-check cards AND the top-level Expected/Found/Delta/Tolerance summary card. Confirm both use this shared class, not two separate untouched components:**
```css
.finding-card__body {
  background: var(--bg-surface);
  border-top: 1px solid var(--border-subtle);
  padding: var(--space-4);
}

.finding-card__values {
  background: var(--bg-elevated);
  border: 1px solid var(--border-subtle);
  border-radius: var(--radius-md);
  padding: var(--space-3) var(--space-4);
}
```
> ⚠️ Verify: if the top summary card (Expected/Found/Delta/Tolerance) is a *separate* component from the per-check finding cards, apply `.finding-card__values` styling (or an equivalent shared class) to it explicitly. Don't assume it inherits this automatically.

#### [MODIFY] `FindingCard.tsx`

**Swap `HelpCircle` → `MinusCircle` for NOT_FOUND:**
```tsx
const OUTCOME_ICON = {
  PASS:               CheckCircle,
  FAIL:               XCircle,
  REVIEW_REQUIRED:    AlertTriangle,
  NOT_FOUND:          MinusCircle,
  NO_APPLICABLE_RULE: MinusCircle,
};
```

**Reviewer action buttons — wrap in the new container, use `btn--reviewer`:**
```tsx
<div className="finding-card__reviewer-actions">
  <button className="btn btn--reviewer">Confirm</button>
  <button className="btn btn--reviewer">Correct</button>
  <button className="btn btn--reviewer">Exception</button>
  <button className="btn btn--reviewer btn--reviewer--dismiss">Dismiss</button>
</div>
```

**Delta value — pair color with BOTH weight and an icon, not color alone:**
```tsx
<span className={`finding-card__value-number finding-card__value-number--delta ${
  finding.outcome === 'FAIL' ? 'finding-card__value-number--fail-bold' : ''
}`}>
  {finding.outcome === 'FAIL' && <TriangleAlert size={12} className="finding-card__delta-icon" />}
  Δ {finding.delta}
</span>
```
```css
.finding-card__value-number--fail-bold { font-weight: var(--weight-bold); }
.finding-card__delta-icon { color: var(--status-fail); margin-right: 2px; vertical-align: -1px; }
```

**Verify mono font is applied everywhere it should be:**
- Confirm `finding.expected`, `finding.found`, `finding.delta`, `finding.tolerance` all render inside a `.finding-card__value-number`-classed span (mono is inherited from that class — don't duplicate the font-family rule elsewhere).

---

### LAYER 5 — ChatInput

#### [MODIFY] `ChatInput.tsx`

**Add "Suggested" label above chips:**
```tsx
<div className="chat-input-area__quick">
  <div className="chat-input-area__quick-header">
    <span className="chat-input-area__quick-label">Suggested</span>
    <div className="chat-input-area__quick-sep" />
  </div>
  {QUICK_PROMPTS.map(p => (...))}
</div>
```

#### [MODIFY] `ChatInput.css`

```css
.chat-input-area__textarea::placeholder { color: var(--text-body); }

.chat-input-area__send { background: var(--color-primary); border-color: var(--color-primary); }
.chat-input-area__send:hover:not(:disabled) { background: var(--color-primary-hover); box-shadow: var(--shadow-primary); }
.chat-input-area__send:focus-visible { outline: none; box-shadow: 0 0 0 3px var(--color-primary-dim); }

.chat-input-area__quick-header {
  display: flex;
  align-items: center;
  gap: var(--space-2);
  margin-bottom: var(--space-1);
}
.chat-input-area__quick-label {
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: var(--tracking-widest);
  color: var(--text-muted);
  font-weight: var(--weight-semi);
  white-space: nowrap;
}
.chat-input-area__quick-sep {
  flex: 1;
  height: 1px;
  background: var(--border-subtle);
}

.chat-input-area__row:focus-within { box-shadow: 0 0 0 3px var(--color-primary-dim); border-color: var(--color-primary-border); }
```

---

### LAYER 6 — Sidebar ID Truncation Fix

#### [MODIFY] `Sidebar.css` and `Sidebar.tsx`

**Wrap IDs at hyphen boundaries only — do NOT use `break-all` (it breaks mid-digit unpredictably):**
```css
.sidebar__thread-id {
  white-space: normal;
  overflow-wrap: break-word;   /* break-all removed — snaps mid-character otherwise */
  font-size: var(--text-xs);
  font-family: var(--font-mono);
}

.sidebar__thread-vendor {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  max-width: 100%;
}
```

**In `Sidebar.tsx`, insert soft break hints at hyphens so wrapping is predictable rather than left to the browser:**
```tsx
// Instead of rendering pkg.id directly, split and join with <wbr>
{pkg.id.split('-').map((part, i, arr) => (
  <React.Fragment key={i}>
    {part}{i < arr.length - 1 && <>-<wbr /></>}
  </React.Fragment>
))}
```
This guarantees `PKG-2026-001` wraps as `PKG-2026-` / `001`, never mid-digit.

**Widen sidebar slightly for 2-line IDs:**
```css
/* In tokens.css */
--sidebar-width: 264px; /* was 248px */
```

---

### LAYER 7 — Badge Icon (NOT FOUND → neutral)

#### [MODIFY] `Badge.tsx`

```tsx
NOT_FOUND: { label: 'NOT FOUND', cls: 'badge--missing', dot: '–' },
```

---

## What Is NOT Being Changed

- Layout structure (column order, IDE/Standard split — already correct)
- Color theme palette (crimson brand color stays for accents, brand bar, selected state, and genuinely destructive actions)
- Any backend or mock data
- Mascot icon — pending the Layer 0 incognito check; if confirmed external/unrelated, no fix needed

---

## Verification Plan

### Visual Checklist
- [ ] Sign Off button is **blue**, not red
- [ ] All other non-destructive primary CTAs found in the Layer 0 grep are also blue, not just Sign Off
- [ ] FAIL delta Δ value is **18px bold with a warning icon**, labels are **10px** — hierarchy holds without relying on color alone
- [ ] FAIL card has red left accent, PASS row is visually muted
- [ ] Confirm/Correct/Exception/Dismiss have **visible borders** and sit inside a shared container (read as one toolbar)
- [ ] Chat input placeholder is readable (not near-invisible)
- [ ] Send button is **blue** and shows an active state when text is typed
- [ ] Chip row shows **"Suggested"** label with separator
- [ ] Sidebar PKG IDs **wrap at hyphens only** — no mid-digit breaks
- [ ] NOT_FOUND icon is `MinusCircle`, badge dot is `–`
- [ ] Progress bar fill is **blue**, not crimson
- [ ] **Both** the top summary card and per-check finding cards have surface elevation (bg-surface/bg-elevated + border)
- [ ] Reason text has **loose line-height** and breathing margin

### Accessibility Checklist (new — was missing from v2)
- [ ] `--color-primary` and `--color-primary-hover` pass 4.5:1 contrast with white text, in both light and dark mode
- [ ] `--text-muted` passes 4.5:1 in both light and dark mode (check separately — don't assume the light-mode fix carries over)
- [ ] Tab through the reviewer action buttons, chat send button, and Sign Off — every one shows a visible `:focus-visible` ring
- [ ] Run axe DevTools or Lighthouse accessibility audit on the ReviewPage route; resolve any new violations
- [ ] Confirm delta/FAIL state is distinguishable with a browser colorblindness simulator (not just color — icon/weight should carry the signal independently)

### Run dev server
```bash
npm run dev  # http://localhost:5174/
```
