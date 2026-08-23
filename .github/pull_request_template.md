<!--
Keep this short. The issue holds the spec and the plan; this says what changed and what proves it.
-->

## Summary

-

## Why

<!-- What this makes possible, or what it fixes. One or two sentences. -->

## Local CI

<!--
Required. Actions minutes bill against a private-repo quota and may be unavailable, so the local run
is the primary evidence — see AGENTS.md §0. Paste the final summary line from `make ci`.
-->

```
$ make ci
...
```

- [ ] `make ci` passed, and the final line is pasted above
- [ ] Anything it reported as **skipped** is either resolved, or explained below
- [ ] No check was weakened to get a green run — no `continue-on-error`, no new `# type: ignore` or
      `# noqa` over a real problem, no test deleted, skipped or loosened

**Skipped or not run, and why:**

<!--
Be specific. The two common ones:
  - the PostgreSQL suite skipped because Docker was not running (486 tests)
  - board drift skipped because `gh` was not authenticated
Both are honest answers. "None" is fine when it is true.
-->

## Verification

<!--
What proves this works, beyond the suite going green. Name the test that would fail if the change
were reverted. For anything touching verdict/, rules/ or evidence/, name the failure-mode tests:
missing operand, ambiguous input, unit mismatch, boundary-exact on both sides.
-->

## Anything the reviewer should push back on

<!--
Optional, and the most useful box on the form. A decision you were unsure about, an interface you
invented because the design did not cover it, a test you could not write. If the design document
disagreed with the issue's plan, say so here — the design wins, and the issue is what gets amended.
-->

<!--
Closes #<issue>
-->
