/**
 * No legacy class name turns into a Tailwind utility (#1029).
 *
 * Tailwind generates a utility for any word in the source that matches one of its class names. A
 * legacy element with `className="text-muted"` would then pick up Tailwind's `text-muted` from the
 * utilities layer, which beats the legacy layer — the old page restyled by accident. This compiles
 * every legacy class name through the real Tailwind config and fails on any match not listed below.
 */
import fs from 'node:fs';
import path from 'node:path';
import { compile } from '@tailwindcss/node';
import { describe, expect, it } from 'vitest';

const ROOT = path.resolve(__dirname, '../..');
const SRC = path.join(ROOT, 'src');

/**
 * Collisions known to be harmless. Each legacy rule and the Tailwind utility do the same thing:
 * - sr-only, truncate: same declarations (sr-only adds clip-path to the legacy clip; still hidden).
 * - col-span-N: legacy grid helpers with Tailwind's exact meaning (grid-column: span N / span N).
 */
const HARMLESS = new Set(['sr-only', 'truncate', ...Array.from({ length: 12 }, (_, i) => `col-span-${i + 1}`)]);

function walk(dir: string): string[] {
  return fs.readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    return entry.isDirectory() ? walk(full) : [full];
  });
}

/** Files written before the redesign: everything except shadcn/ui, its helpers and the UI kit. */
function isLegacy(file: string): boolean {
  const rel = path.relative(SRC, file).split(path.sep).join('/');
  if (rel.startsWith('styles/') || rel.startsWith('lib/') || rel.startsWith('hooks/')) return false;
  if (rel.startsWith('pages/ui-kit/') || rel.startsWith('components/data-table/')) return false;
  // shadcn files are kebab-case lower-case; legacy components are PascalCase or camelCase.
  if (rel.startsWith('components/ui/') && /^[a-z0-9-]+\.tsx$/.test(path.basename(rel))) return false;
  return true;
}

function legacyClassNames(): Set<string> {
  const names = new Set<string>();
  for (const file of walk(SRC).filter(isLegacy)) {
    const text = fs.readFileSync(file, 'utf8');
    if (file.endsWith('.css')) {
      for (const m of text.replace(/\/\*[\s\S]*?\*\//g, '').matchAll(/\.(-?[A-Za-z_][\w-]*)/g)) names.add(m[1]);
    } else if (/\.tsx?$/.test(file)) {
      // Class names in JSX and in the strings that build them (ternaries, arrays, template parts).
      for (const m of text.matchAll(/['"`]([^'"`\n]*)['"`]/g)) {
        for (const token of m[1].split(/\s+/)) if (/^-?[a-z][\w-]*$/.test(token)) names.add(token);
      }
    }
  }
  return names;
}

describe('legacy class names and Tailwind', () => {
  it('no legacy class name is generated as a Tailwind utility, except the known harmless ones', async () => {
    const entry = fs.readFileSync(path.join(SRC, 'styles/tailwind.css'), 'utf8');
    const compiler = await compile(entry, { base: path.join(SRC, 'styles'), onDependency: () => {} });
    const css = compiler.build([...legacyClassNames()]);

    const utilities = css.slice(css.indexOf('@layer utilities'));
    const generated = new Set(
      [...utilities.matchAll(/\.((?:\\.|[\w-])+)/g)].map((m) => m[1].replace(/\\/g, '')),
    );
    const legacyCss = new Set<string>();
    for (const file of walk(SRC).filter((f) => isLegacy(f) && f.endsWith('.css'))) {
      for (const m of fs.readFileSync(file, 'utf8').matchAll(/\.(-?[A-Za-z_][\w-]*)/g)) legacyCss.add(m[1]);
    }
    const legacyTsx = new Set<string>();
    for (const file of walk(SRC).filter((f) => isLegacy(f) && f.endsWith('.tsx'))) {
      for (const m of fs.readFileSync(file, 'utf8').matchAll(/className=\{?['"`]([^'"`]*)['"`]/g)) {
        m[1].split(/\s+/).forEach((t) => t && legacyTsx.add(t));
      }
    }
    // A collision is a generated utility whose name a legacy rule styles or a legacy element wears.
    const collisions = [...generated].filter((name) => legacyCss.has(name) || legacyTsx.has(name));
    expect(collisions.filter((name) => !HARMLESS.has(name)).sort()).toEqual([]);
  });

  it('text-muted, which the legacy pages use, is never generated', async () => {
    const entry = fs.readFileSync(path.join(SRC, 'styles/tailwind.css'), 'utf8');
    const compiler = await compile(entry, { base: path.join(SRC, 'styles'), onDependency: () => {} });
    expect(compiler.build(['text-muted'])).not.toMatch(/\.text-muted\s*\{/);
  });
});
