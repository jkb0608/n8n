#!/usr/bin/env node
/**
 * Assert the per-Code-node jsCode LINE COUNT contract.
 *
 * Why this exists next to jsast_equiv.mjs: the AST proof only says "semantics are
 * unchanged vs the baseline" - it cannot say "the comments are still there".
 * A workflow compared against its own baseline passes trivially, which is exactly
 * how a whole-file import built from a STALE snapshot silently reverted twelve
 * annotated nodes while `verify` still printed PASS. Line counts are the cheap,
 * explicit contract that catches that class of mistake.
 *
 * Usage:
 *   node tools/expect_lines.mjs <dump.json> <expect.json>
 *
 * <expect.json> is {"[node name]": <expected line count>, ...} - generate it from
 * the file you proved locally, then assert it against the workflow you just wrote.
 * Exit 0 = every asserted node matches.
 */
import { readFileSync } from 'node:fs';

const [dumpPath, expectPath] = process.argv.slice(2);
if (!dumpPath || !expectPath) {
  console.error('usage: expect_lines.mjs <dump.json> <expect.json>');
  process.exit(2);
}
const raw = JSON.parse(readFileSync(dumpPath, 'utf8'));
const wf = Array.isArray(raw) ? raw[0] : raw;
const expect = JSON.parse(readFileSync(expectPath, 'utf8'));

const bad = [];
for (const [name, want] of Object.entries(expect)) {
  const node = (wf.nodes || []).find((x) => x.name === name);
  if (!node) { bad.push(`${name}: MISSING`); continue; }
  const got = String((node.parameters || {}).jsCode || '').split('\n').length;
  if (got !== want) bad.push(`${name}: expected ${want} lines, got ${got}`);
}
if (bad.length) {
  console.log('EXPECT-LINES: FAIL');
  for (const b of bad) console.log('  - ' + b);
  process.exit(1);
}
console.log(`EXPECT-LINES: OK (${Object.keys(expect).length} nodes asserted)`);
