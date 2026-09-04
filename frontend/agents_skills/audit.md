Here's the consolidated design audit — copy this whole block as context for your fix prompt:

---

**DESIGN AUDIT — Graniti Vicentia Review Platform (Package Review Screen)**

**Color & Semantic Issues**
- Primary CTA "Sign Off" button uses red/pink, the same hue family as error states (red delta text, FAIL badges). Red should be reserved exclusively for errors/failures; give primary actions a distinct brand color (blue/teal/green).
- Status is conveyed by color alone in several places (delta value, bullet dots next to finding labels) — fails accessibility for colorblind users. Pair every color-coded status with an icon or weight change, not color alone.
- Secondary label text (REVIEWS, CALCULATION TRACE, EXPECTED/FOUND/DELTA/TOLERANCE) is very low contrast gray, likely failing WCAG AA (4.5:1) especially in light mode.
- Three different icon languages are used for status (checkmark, warning triangle, question mark for "NOT FOUND") when there are really only two concepts (pass, needs-attention/unknown). Question mark reads as "error" when it should read as "neutral/missing." Consolidate to a consistent icon system.

**Typography & Hierarchy**
- No dominant heading: package ID, client/project name, and location subtitle are all near-identical in size/weight, giving the eye no clear entry point. Make the client/project name the true H1; demote package ID and subtitle to smaller/lighter secondary text.
- Critical data values (Found: 5980mm, Delta: 32mm) are only marginally larger than their labels (TOLERANCE, EXPECTED). The most important numbers in a QA tool should read at ~1.5–2x label size to draw the eye first.
- All five findings-list rows (CT-2, CT-3a, CT-3b, CAB-1, CAB-2) share identical font size/weight/row height regardless of status. Rows needing attention should visually interrupt the pattern (heavier weight/larger badge) while passing rows recede.
- Top bar: "0 / 4 reviewed" status text and "Sign Off" button read at similar visual weight, but informational status should be quieter and the CTA should dominate.
- Line-height on the finding description sentence ("Measured width exceeds tolerance...") is too tight relative to the spacing used elsewhere on the page — feels cramped next to the data row above and the Calculation Trace link below.
- No defined type scale — most text clusters in a narrow 12–16px range regardless of importance. Needs an explicit scale (e.g., 12/14/16/20/28px) mapped to actual content priority.

**Spacing & Layout Structure**
- Inconsistent vertical rhythm across sections: the data card (Expected/Found/Delta/Tolerance) is loosely spaced, the findings list is tightly packed, and the chat bar area is loose again — creates a bolted-together feel rather than one cohesive layout. Apply a consistent spacing scale (e.g., 4/8/16/24/32px) throughout.
- No card/surface separation between the comparison table, finding detail, and findings list — everything sits on one flat background with no border, shadow, or elevation to distinguish sections.
- Sidebar package IDs are truncated ("PKG-2026-...", "PKG-202...") — the primary scan value gets clipped. Widen the sidebar, wrap to two lines (ID + status badge stacked), or truncate the client name instead of the ID.

**Component & Interaction Issues**
- Confirm / Correct / Exception / Dismiss action buttons have no visible button chrome (no border/fill/hover state) — they read as plain inline text, so users may not recognize them as the core review actions. Needs proper button styling (segmented control or clearly bounded ghost buttons), sized to reflect their priority over secondary actions like "View Evidence."
- "0 / 4 reviewed" has no visual progress indicator, just text. Add a progress bar or segmented dots for at-a-glance completion status.
- Suggested prompt chips ("Run full review," "Show FAIL findings only," etc.) sit directly above the chat input with no label clarifying they're AI suggestions vs. persistent toolbar actions. Add a "Suggested" label or visually separate them from the input.
- Chat input placeholder text and send icon both look disabled/inactive even when presumably functional. Increase placeholder contrast and give the send icon a clear active state.
- Mascot icon (bottom-left) overlaps the "Rulebook" nav item and is clipped by the viewport edge in both light and dark mode — appears to be a positioning bug, and is tonally mismatched with the rest of the technical/enterprise UI.

**General Direction**
Define an explicit type scale and spacing scale up front, then map every element to a step on those scales based on actual data priority (critical delta/found values > finding status > metadata > labels) rather than what currently just "fits." Establish one consistent semantic color system where red = error only, and a primary brand color = primary actions.



**1. Sidebar text truncation**
"PKG-2026-..." and "PKG-202..." — the package IDs, the one thing a user scans this list for, get clipped. Fix: either widen the sidebar, drop to a two-line list item (ID on its own line, status badge below), or truncate the *client name* instead of the ID.

**2. Sign Off button uses the "danger" color**
The primary CTA is pink/red — the same hue family as your error states (Δ 32mm in red, FAIL badges). That's a conflicting signifier: red usually means "stop/wrong," not "confirm and proceed." Fix: give primary actions a distinct brand color (blue/teal/green) and reserve red exclusively for errors/failures.

**3. Confirm / Correct / Exception / Dismiss read as plain text, not buttons**
They're sitting inline with no button chrome — no border, fill, or hover affordance visible. Users may not realize these are the actual review actions. Fix: turn these into a proper button group (segmented control or ghost buttons with visible boundaries).

**4. No card separation between sections**
The comparison table (Expected/Found/Delta), the finding detail, and the findings list all sit on the same flat white/black surface with no border, shadow, or background shift. It reads as one continuous block instead of distinct components. Fix: give each section its own card surface with subtle elevation or a hairline border.

**5. Low-contrast label text**
"REVIEWS," "CALCULATION TRACE," "EXPECTED/FOUND/DELTA/TOLERANCE" labels are very light gray — likely failing WCAG AA contrast, especially in light mode. Fix: darken secondary label text to at least a 4.5:1 ratio against the background.

**6. Status meaning conveyed by color alone in places**
The delta value (Δ 32 mm) is red text with no icon/weight change — colorblind users lose that signal. Same issue with the small bullet dot next to each finding label (CT-2 •, CAB-2 •) — it's the same neutral color regardless of pass/fail state, so it's decorative noise rather than an indicator. Fix: pair color with an icon or bold/weight change; remove the meaningless bullet or make it actually reflect status.

**7. Ambiguous iconography for "NOT FOUND"**
CT-3b uses a "?" icon — is that "unknown/needs input" or "error"? It's a third icon type alongside the check and warning-triangle, splitting user attention across three icon languages for what's really two concepts (pass, needs-attention). Fix: consolidate to check / warning, and use a distinct neutral (dash or empty-circle) for not-found rather than a question mark that reads as "problem."

**8. "0 / 4 reviewed" has no visual progress**
It's just text. Fix: a thin progress bar or segmented dots would let users gauge review completion at a glance, especially useful across multiple packages in the sidebar.

**9. Suggestion chips float without context**
"Run full review," "Show FAIL findings only," etc. sit directly above the chat input with no label distinguishing them as AI prompt suggestions vs. persistent toolbar actions. Fix: a small "Suggested" or "Try asking" label above the chip row, or visually detach them from the input field.

**10. Chat input looks disabled**
Placeholder text and a faint gray send icon both read as inactive/disabled, even though the field is presumably usable. Fix: darken the placeholder and give the send icon an active-state color once text is entered.

**11. Off-brand mascot clipped in the corner**
The rainbow cat/panda icon bottom-left overlaps the "Rulebook" nav item and gets cut off by the viewport edge in both screenshots. For an enterprise compliance tool, it also feels tonally mismatched with the rest of the (serious, technical, monospace-heavy) UI. Fix: reposition it out of the way of nav items, or drop it — it's not adding wayfinding value and is currently just visual clutter/bug.

**12. Two different type systems without clear rationale**
Package IDs and check codes (CT-2, CAB-1) are monospace; everything else is sans-serif. That's a reasonable convention for "data/code" vs. "prose," but it's not applied consistently — worth double-checking every technical value (mm figures, tolerances) follows the same rule so the pattern reads as intentional rather than random.



**1. Not enough size contrast on the most important number**
"5980 mm" (the actual failing measurement) and "Δ 32 mm" are only marginally larger than the "TOLERANCE ± 3.175 mm" label next to them. In a QA tool, the delta/found values *are* the payload — they should be reading at a noticeably larger scale (maybe 1.5–2x the label size) so the eye lands there first. Right now everything in that data row sits in a narrow 12–16px band, so nothing pops.

**2. Package title has no dominant heading**
"PKG-2026-001" and "Elite Stone Works" are nearly identical in weight/size, with "Marriott Houston — Vanity Countertops" right below at almost the same size too. Three lines of near-equal-weight text stacked = no entry point for the eye. Fix: make one of these — probably the client/project name — the actual H1 (larger, bolder), and drop the package ID + subtitle to a smaller, lighter secondary line.

**3. Findings list rows are flat — no rhythm**
CT-2, CT-3a, CT-3b, CAB-1, CAB-2 are all the same row height, same font size for the code chip and the label, same badge size. Five rows of identical visual weight makes users read linearly instead of scanning for what matters (the REVIEW/warning ones should visually interrupt the pattern, not blend into it). Fix: either increase weight/size on non-passing rows, or reduce visual noise on the passing ones (lighter text, smaller badge) so contrast does the sorting for you.

**4. Padding/density mismatch between sections**
The top data card (Expected/Found/Delta/Tolerance) is fairly airy with generous spacing, but the findings list right below it switches to a tighter, denser row structure with less padding — then the chat bar at the bottom goes airy again. That inconsistent rhythm (loose → tight → loose) makes the page feel like three separate components bolted together rather than one designed layout. Fix: pick a consistent vertical spacing scale (e.g., 8/16/24px system) and apply it uniformly across sections.

**5. Action buttons undersized relative to their importance**
Confirm/Correct/Exception/Dismiss are small, same font size as body copy, in a row that's visually equal to or smaller than the "View Evidence" button next to them — but they're the core review actions, while View Evidence is a secondary/reference action. The sizing doesn't match the actual task priority.

**6. Top bar elements compete at the same scale**
"0 / 4 reviewed" and the "Sign Off" button sit at roughly the same visual weight in the top right. Progress status is informational (should be quieter/smaller) while Sign Off is the primary CTA (should dominate). Right now they're fighting for the same amount of attention.

**7. Line-height too tight on the description sentence**
"Measured width exceeds tolerance. Shop drawing shows 5980 mm..." sits very close to the data row above it and the "Calculation Trace" link below — the line-height/margin isn't giving it breathing room proportional to its font size, so it reads cramped relative to the more generous spacing elsewhere on the page.

**The underlying fix:** define a type scale (e.g., 12 / 14 / 16 / 20 / 28px) and a spacing scale (e.g., 4 / 8 / 16 / 24 / 32px) up front, then map every element to a step on those scales based on actual importance — not just what fits. Right now most elements cluster in the 12–16px / tight-padding range regardless of role, which is why hierarchy feels flat even though the data itself has a clear priority order (critical delta > finding status > metadata > labels).

