#!/usr/bin/env node
/**
 * Prove that an edit to the workflow's Code nodes changed only comments and
 * formatting.
 *
 * Why a real parser: these jsCodes contain `https://api.deepseek.com` and
 * `http://127.0.0.1:8080/...`, so a regex that strips `//` comments will happily
 * truncate those lines and then "prove" anything you like. acorn drops comments
 * and whitespace by construction, so comparing ASTs is the honest check.
 *
 * Parser: the acorn bundled with the local n8n install (no network, no new dep).
 * Override with ACORN_PATH=/path/to/acorn if yours lives elsewhere.
 *
 * What is compared, per Code node:
 *   ast_full  - canonical AST including each literal's `raw` text (strongest)
 *   ast_sem   - same with `raw` dropped (semantics only, quote style ignored)
 *   literals  - sorted list of every string / regex literal VALUE (decoded)
 *   prefix    - [PROMPT]'s frozen prefix FNV-1a plus SYS / D_TAIL literal hashes
 * and workflow-wide:
 *   noncode   - whole workflow with every jsCode blanked, so any change to
 *               wiring, positions, parameters, credentials or disabled flags fails
 *
 * Usage (both exports come from `n8n export:workflow --id=... --output=...`):
 *   node tools/jsast_equiv.mjs baseline --dump dump/baseline/wf_baseline.json --out dump/jsast_baseline.json
 *   node tools/jsast_equiv.mjs verify   --baseline dump/jsast_baseline.json --dump dump/wf.json
 *   node tools/jsast_equiv.mjs selftest --dump dump/baseline/wf_baseline.json
 *
 * Exit code 0 = all checks identical.
 */
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
const ACORN_PATH = process.env.ACORN_PATH
  || 'C:/Users/Administrator/AppData/Roaming/npm/node_modules/n8n/node_modules/acorn';
const acorn = require(ACORN_PATH);

const CODE_TYPE = 'n8n-nodes-base.code';
// n8n rewrites these on every save: version bookkeeping + runtime artifacts.
// They are not workflow content, so they must not count as "the workflow changed".
const VOLATILE = ['versionId', 'activeVersionId', 'updatedAt', 'createdAt',
  'updatedBy', 'createdBy', 'triggerCount', 'pinData', 'versionCounter', 'versionMetadata'];

const sha = (s) => createHash('sha256').update(s, 'utf8').digest('hex').slice(0, 16);
const canon = (v) => JSON.stringify(v);

function parseCode(src, label) {
  try {
    return acorn.parse(src, {
      ecmaVersion: 'latest',
      sourceType: 'script',
      allowReturnOutsideFunction: true,
      allowAwaitOutsideFunction: true,
    });
  } catch (e) {
    throw new Error(`parse failed for ${label}: ${e.message}`);
  }
}

/** Depth-first canonicalisation: drop positions (and optionally `raw`). */
function normNode(node, dropRaw, out) {
  if (Array.isArray(node)) {
    for (const child of node) normNode(child, dropRaw, out);
    return;
  }
  if (node === null || typeof node !== 'object') {
    out.push(node);
    return;
  }
  const keys = Object.keys(node).filter((k) => k !== 'start' && k !== 'end' && k !== 'loc'
    && !(dropRaw && k === 'raw')).sort();
  out.push('{');
  for (const k of keys) {
    out.push(k);
    if (k === 'regex' && node[k] && typeof node[k] === 'object') {
      out.push(node[k].pattern, node[k].flags);
    } else {
      normNode(node[k], dropRaw, out);
    }
  }
  out.push('}');
}

function astHash(ast, dropRaw) {
  const out = [];
  normNode(ast, dropRaw, out);
  return sha(out.join('\u0001'));
}

/** Walk the AST, collecting literal values (strings + regex sources). */
function collectLiterals(ast) {
  const lits = [];
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== 'object') return;
    if (node.type === 'Literal') {
      if (typeof node.value === 'string') lits.push('s:' + node.value);
      else if (node.regex) lits.push('r:/' + node.regex.pattern + '/' + node.regex.flags);
    }
    if (node.type === 'TemplateLiteral') {
      lits.push('t:' + node.quasis.map((q) => q.value.cooked).join('\u0000'));
    }
    for (const k of Object.keys(node)) if (k !== 'start' && k !== 'end') walk(node[k]);
  })(ast);
  return lits;
}

/** [PROMPT]'s frozen prefix: read SYS / D_TAIL from the AST, then FNV-1a them. */
function prefixInfo(ast) {
  const found = {};
  (function walk(node) {
    if (Array.isArray(node)) return node.forEach(walk);
    if (!node || typeof node !== 'object') return;
    if (node.type === 'VariableDeclarator' && node.id && node.id.name
      && node.init && node.init.type === 'Literal' && typeof node.init.value === 'string') {
      if (node.id.name === 'SYS' || node.id.name === 'D_TAIL') found[node.id.name] = node.init.value;
    }
    for (const k of Object.keys(node)) if (k !== 'start' && k !== 'end') walk(node[k]);
  })(ast);
  if (!found.SYS || !found.D_TAIL) return { hash: null, sys: null, dtail: null };
  const prefix = found.SYS + found.D_TAIL;
  let h = 0x811c9dc5;
  for (let i = 0; i < prefix.length; i++) {
    h ^= prefix.charCodeAt(i);
    // NOTE: deliberately mirrors the node, including its float multiply. The node
    // uses `h * 0x01000193` (a double, so the product loses precision above 2^53)
    // and not Math.imul; reproducing that exactly is what makes this offline value
    // comparable with the prefix_hash the workflow prints at runtime.
    h = (h * 0x01000193) >>> 0;
  }
  return { hash: ('00000000' + h.toString(16)).slice(-8), sys: sha(found.SYS), dtail: sha(found.D_TAIL) };
}

function loadWf(p) {
  const data = JSON.parse(readFileSync(p, 'utf8'));
  return Array.isArray(data) ? data[0] : data;
}

function snapshot(wf) {
  const nodes = wf.nodes || [];
  const conns = wf.connections || {};
  let edges = 0;
  const srcs = new Set();
  for (const [from, outs] of Object.entries(conns)) {
    for (const gs of Object.values(outs)) {
      for (const g of gs) { edges += (g || []).length; if ((g || []).length) srcs.add(from); }
    }
  }
  const flow = nodes.filter((n) => n.type !== 'n8n-nodes-base.stickyNote').map((n) => n.name);
  const clean = {};
  for (const [k, v] of Object.entries(wf)) if (!VOLATILE.includes(k)) clean[k] = v;
  clean.nodes = nodes.map((n) => {
    const copy = {};
    for (const [k, v] of Object.entries(n)) if (!VOLATILE.includes(k)) copy[k] = v;
    if (copy.type === CODE_TYPE && copy.parameters) {
      const p = { ...copy.parameters };
      if ('jsCode' in p) p.jsCode = '<BLANKED>';
      copy.parameters = p;
    }
    return copy;
  });
  const codeNodes = {};
  for (const n of nodes.filter((x) => x.type === CODE_TYPE)) {
    const src = (n.parameters || {}).jsCode || '';
    const ast = parseCode(src, n.name);
    const lits = collectLiterals(ast);
    const pref = n.name.startsWith('[PROMPT]') ? prefixInfo(ast) : { hash: null, sys: null, dtail: null };
    codeNodes[n.name] = {
      ast_full: astHash(ast, false),
      ast_sem: astHash(ast, true),
      literals: sha(lits.slice().sort().join('\u0000')),
      literal_count: lits.length,
      prefix: pref.hash,
      sys: pref.sys,
      dtail: pref.dtail,
      raw_len: src.length,
      lines: src.split('\n').length,
    };
  }
  return {
    flow_nodes: flow.length,
    edges,
    terminals: flow.filter((n) => !srcs.has(n)).sort(),
    noncode: sha(canon(clean)),
    code_nodes: codeNodes,
    acorn: acorn.version,
  };
}

function main() {
  const [cmd, ...rest] = process.argv.slice(2);
  const arg = (name, dflt) => {
    const i = rest.indexOf('--' + name);
    return i >= 0 ? rest[i + 1] : dflt;
  };
  const dump = arg('dump');
  if (!dump) { console.error('missing --dump'); return 2; }

  if (cmd === 'baseline') {
    const out = arg('out');
    const snap = snapshot(loadWf(dump));
    writeFileSync(out, JSON.stringify(snap, null, 2), 'utf8');
    console.log(`acorn ${snap.acorn} | baseline -> ${out}`);
    console.log(`flow_nodes=${snap.flow_nodes} edges=${snap.edges} terminals=${snap.terminals}`);
    console.log(`noncode=${snap.noncode} | code nodes=${Object.keys(snap.code_nodes).length}`);
    for (const [n, v] of Object.entries(snap.code_nodes)) {
      console.log(`  ${n.padEnd(32)} ast=${v.ast_full} lits=${v.literals} lines=${v.lines}${v.prefix ? ' prefix=' + v.prefix : ''}`);
    }
    return 0;
  }

  if (cmd === 'selftest') {
    const wf = loadWf(dump);
    const base = snapshot(wf);
    const node = wf.nodes.find((n) => n.type === CODE_TYPE);
    const orig = node.parameters.jsCode;
    // (1) comment + reindent only -> AST must stay identical
    const cosmetic = '// 头部注释\n' + orig.split('\n').map((l) => (l.trim() ? '    ' + l : l)).join('\n') + '\n/* 尾部 */\n';
    node.parameters.jsCode = cosmetic;
    const afterCosmetic = snapshot(wf).code_nodes[node.name];
    // (2) real change inside a string literal -> must be detected
    node.parameters.jsCode = orig.replace(/'([^']*)'/, "'$1CHANGED'");
    const afterLiteral = snapshot(wf).code_nodes[node.name];
    // (3) real change to an operator -> must be detected
    const opMutations = [['===', '!=='], ['||', '&&'], [' + 1', ' + 2'], ['>=', '>'], ['=== true', '=== false']];
    let opMutated = null;
    for (const [a, b] of opMutations) {
      if (orig.includes(a)) { opMutated = orig.replace(a, b); break; }
    }
    node.parameters.jsCode = opMutated;
    const afterOperator = snapshot(wf).code_nodes[node.name];
    node.parameters.jsCode = orig;
    const b = base.code_nodes[node.name];
    const okCosmetic = afterCosmetic.ast_full === b.ast_full && afterCosmetic.literals === b.literals;
    const okLiteral = afterLiteral.ast_full !== b.ast_full && afterLiteral.literals !== b.literals;
    const okOperator = opMutated !== null && afterOperator.ast_full !== b.ast_full;
    console.log(`selftest on ${node.name} (acorn ${base.acorn})`);
    console.log(`  comment+reindent only  -> AST ${okCosmetic ? 'identical (correct: cosmetic)' : 'DIFF (WRONG)'}`);
    console.log(`  string literal changed -> ${okLiteral ? 'detected (correct)' : 'MISSED (WRONG)'}`);
    console.log(`  operator changed       -> ${okOperator ? 'detected (correct)' : 'MISSED (WRONG)'}`);
    const pass = okCosmetic && okLiteral && okOperator;
    console.log(pass ? 'SELFTEST: PASS (proof has teeth in both directions)' : 'SELFTEST: FAIL');
    return pass ? 0 : 1;
  }

  // verify
  const base = JSON.parse(readFileSync(arg('baseline'), 'utf8'));
  const cur = snapshot(loadWf(dump));
  const fails = [];
  for (const k of ['flow_nodes', 'edges', 'terminals', 'noncode']) {
    if (canon(base[k]) !== canon(cur[k])) fails.push(`${k}: ${canon(base[k])} -> ${canon(cur[k])}`);
  }
  console.log(`${'code node'.padEnd(34)} ${'ast_full'.padEnd(9)} ${'ast_sem'.padEnd(9)} ${'literals'.padEnd(9)} prefix   lines`);
  for (const [name, b] of Object.entries(base.code_nodes)) {
    const c = cur.code_nodes[name];
    if (!c) { fails.push('missing code node: ' + name); continue; }
    const a1 = b.ast_full === c.ast_full;
    const a2 = b.ast_sem === c.ast_sem;
    const l = b.literals === c.literals;
    const p = b.prefix === c.prefix && b.sys === c.sys && b.dtail === c.dtail;
    if (!a1) fails.push('AST changed: ' + name);
    if (!a2) fails.push('AST (semantic) changed: ' + name);
    if (!l) fails.push('literal value changed: ' + name);
    if (!p) fails.push('frozen prefix changed: ' + name);
    console.log(`${name.padEnd(34)} ${(a1 ? 'OK' : 'DIFF').padEnd(9)} ${(a2 ? 'OK' : 'DIFF').padEnd(9)} ${(l ? 'OK' : 'DIFF').padEnd(9)} ${(p ? 'OK' : 'DIFF').padEnd(8)} ${b.lines} -> ${c.lines}`);
  }
  const extra = Object.keys(cur.code_nodes).filter((n) => !base.code_nodes[n]);
  if (extra.length) fails.push('new code node(s): ' + extra.join(', '));
  console.log();
  const pn = '[PROMPT] 组装提示词';
  console.log(`flow_nodes=${cur.flow_nodes} edges=${cur.edges} terminals=${cur.terminals} noncode=${cur.noncode}`);
  console.log(`prefix_hash=${cur.code_nodes[pn] && cur.code_nodes[pn].prefix} (baseline ${base.code_nodes[pn] && base.code_nodes[pn].prefix})`);
  console.log();
  if (fails.length) { console.log('RESULT: FAIL'); fails.forEach((f) => console.log('  - ' + f)); return 1; }
  console.log('RESULT: PASS - AST identical, literal values identical, frozen prefix identical,');
  console.log('        and every non-jsCode field of the workflow byte-identical.');
  return 0;
}

process.exit(main());
