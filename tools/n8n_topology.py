#!/usr/bin/env python3
"""n8n workflow topology gate: snapshot (redact) + lint.

Why: n8n cannot detect accidental cycles. Loops are legal there (the bounded
retry back-edge is intentional), so the built-in validator only reports local
problems - missing parameters, disconnected nodes, group shape. A leftover edge
that closes a silent cycle (observed: ``[MAIL] -> [OUT]`` on a chain that already
runs ``[OUT] -> [TPL] -> [MAIL]``) sends duplicate emails until SMTP throttles.
That is a topology-semantics bug; only a graph check finds it.

Commands:
    python tools/n8n_topology.py snapshot --dump dump/ --out topology/
    python tools/n8n_topology.py lint topology/
    python tools/n8n_topology.py self-test

Never commit a raw ``n8n export`` dump: it carries API keys, Basic auth headers
and parameter values in clear text. `snapshot` reduces it to name/type/wiring.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Expected terminals: nodes of the execution flow that may have no main outgoing
# edge. Declared per workflow because a workflow's sinks are a design decision.
EXPECTED_TERMINALS = {"[MAIL] 发邮件通知"}

# Edges allowed to close a cycle. Only deliberately bounded loops belong here.
ALLOWED_BACK_EDGES = {
    # poll loop: exits when the build stops reporting building=true
    "[IF] 还在构建? -> [WAIT] 等 30 秒",
    # TDD retry loop: exits when retry_count reaches max_retries
    "[IF] 还有重试额度? -> [CI] 触发 Jenkins 构建",
}

CONDITIONAL_TYPES = {"n8n-nodes-base.if", "n8n-nodes-base.switch"}
SKIP_TYPES = {"n8n-nodes-base.stickyNote"}
TRIGGER_HINTS = ("trigger", "Trigger", "webhook", "Webhook")


def build_graph(wf: dict):
    """Return (type_by_name, main_edges, duplicates, subnodes).

    Annotations (sticky notes) are dropped. Sub-nodes (LLM models, memories)
    are wired through non-main connection types such as ai_languageModel with
    direction model -> chain; they are not part of execution flow and must not
    be judged as terminals or as unreachable.
    """
    main_edges, dup, seen = [], [], set()
    main_src, main_dst, other_src, other_dst = set(), set(), set(), set()
    for src, outputs in (wf.get("connections") or {}).items():
        for conn_type, groups in outputs.items():
            for out_index, group in enumerate(groups or []):
                for link in (group or []):
                    dst = link.get("node")
                    if dst is None:
                        continue
                    if conn_type == "main":
                        edge = (src, dst, out_index)
                        if edge in seen:
                            dup.append(f"{src} -> {dst} (output {out_index})")
                        seen.add(edge)
                        main_edges.append(edge)
                        main_src.add(src)
                        main_dst.add(dst)
                    else:
                        other_src.add(src)
                        other_dst.add(dst)
    type_by_name = {
        n.get("name"): n.get("type", "")
        for n in wf.get("nodes", [])
        if n.get("type") not in SKIP_TYPES
    }
    subnodes = {
        n for n in type_by_name
        if (n in other_src or n in other_dst) and n not in main_src and n not in main_dst
    }
    type_by_name = {k: v for k, v in type_by_name.items() if k not in subnodes}
    main_edges = [e for e in main_edges if e[0] in type_by_name and e[1] in type_by_name]
    return type_by_name, main_edges, dup, subnodes


def has_cycle(edges) -> bool:
    adj: dict[str, list[str]] = {}
    for src, dst, _ in edges:
        adj.setdefault(src, []).append(dst)
    GRAY, BLACK = 1, 2
    color: dict[str, int] = {}
    found = [False]

    def visit(node: str) -> None:
        color[node] = GRAY
        for nxt in adj.get(node, []):
            state = color.get(nxt, 0)
            if state == GRAY:
                found[0] = True
            elif state == 0:
                visit(nxt)
        color[node] = BLACK

    for node in list(adj):
        if color.get(node, 0) == 0:
            visit(node)
    return found[0]


def scc_cycles(edges):
    adj: dict[str, list[str]] = {}
    for src, dst, _ in edges:
        adj.setdefault(src, []).append(dst)
    index, low, on_stack, stack, counter, out = {}, {}, set(), [], [0], []

    def strong(node: str) -> None:
        index[node] = low[node] = counter[0]
        counter[0] += 1
        stack.append(node)
        on_stack.add(node)
        for nxt in adj.get(node, []):
            if nxt not in index:
                strong(nxt)
                low[node] = min(low[node], low[nxt])
            elif nxt in on_stack:
                low[node] = min(low[node], index[nxt])
        if low[node] == index[node]:
            comp = set()
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.add(w)
                if w == node:
                    break
            if len(comp) > 1:
                out.append(comp)

    for node in list(adj):
        if node not in index:
            strong(node)
    return out


def reachable(type_by_name: dict, edges) -> set:
    adj: dict[str, list[str]] = {}
    for src, dst, _ in edges:
        adj.setdefault(src, []).append(dst)
    queue = [n for n, t in type_by_name.items() if any(h in t for h in TRIGGER_HINTS)]
    seen: set = set()
    while queue:
        node = queue.pop()
        if node in seen:
            continue
        seen.add(node)
        queue.extend(adj.get(node, []))
    return seen


def lint_workflow(name: str, wf: dict, back_edges: set, terminals: set) -> list:
    errors = []
    type_by_name, edges, dup, subnodes = build_graph(wf)
    for d in dup:
        errors.append(f"R5 duplicate edge: {d}")
    present = {f"{s} -> {d}" for s, d, _ in edges}
    for w in sorted(back_edges):
        if w not in present:
            errors.append(f"R1b whitelisted back-edge not present: {w}")
    kept = [e for e in edges if f"{e[0]} -> {e[1]}" not in back_edges]
    if has_cycle(kept):
        errors.append("R1 cycle remains after removing whitelisted back-edges")
    for comp in scc_cycles(edges):
        if not any(type_by_name.get(n, "") in CONDITIONAL_TYPES for n in comp):
            errors.append("R2 unbounded loop (no conditional exits it): " + ", ".join(sorted(comp)))
    sources = {s for s, _, _ in edges}
    unexpected = {n for n in type_by_name if n not in sources} - terminals
    if unexpected:
        errors.append("R3 unexpected terminal node(s): " + ", ".join(sorted(unexpected)))
    unreachable = set(type_by_name) - reachable(type_by_name, edges)
    if unreachable:
        errors.append("R4 unreachable node(s): " + ", ".join(sorted(unreachable)))
    print(f"  nodes={len(type_by_name)} edges={len(edges)} subnodes_skipped={len(subnodes)}")
    return errors


def lint_dir(path: Path) -> int:
    files = sorted(path.rglob("*.json"))
    if not files:
        print(f"no topology json under {path}")
        return 1
    failed = 0
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        workflows = payload.get("workflows") if isinstance(payload, dict) else None
        if not workflows:
            workflows = {f.stem: payload}
        for name, wf in workflows.items():
            print(f"[lint] {f.name} :: {name}")
            errors = lint_workflow(name, wf, ALLOWED_BACK_EDGES, EXPECTED_TERMINALS)
            for e in errors:
                print(f"  ERROR {e}")
            if errors:
                failed += 1
            else:
                print("  OK")
    print(f"[lint] {len(files)} file(s), {failed} failed")
    return 1 if failed else 0


def redact(wf: dict) -> dict:
    nodes = [{"name": n.get("name"), "type": n.get("type")} for n in wf.get("nodes", [])]
    connections = {}
    for src, outputs in (wf.get("connections") or {}).items():
        connections[src] = {
            ctype: [[{"node": l.get("node"), "type": l.get("type"), "index": l.get("index")} for l in (g or [])]
                    for g in (groups or [])]
            for ctype, groups in outputs.items()
        }
    out = {"name": wf.get("name"), "nodes": nodes, "connections": connections}
    keep = ("executionTimeout", "executionOrder", "errorWorkflow")
    if wf.get("settings"):
        out["settings"] = {k: v for k, v in wf["settings"].items() if k in keep}
    return out


def snapshot(dump_dir: Path, out_dir: Path) -> int:
    files = sorted(dump_dir.rglob("*.json"))
    if not files:
        print(f"no export json under {dump_dir}")
        return 1
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for f in files:
        payload = json.loads(f.read_text(encoding="utf-8"))
        for wf in (payload if isinstance(payload, list) else [payload]):
            if not isinstance(wf, dict) or not wf.get("nodes"):
                continue
            name = (wf.get("name") or f.stem).strip().replace("/", "_").replace(" ", "-")
            (out_dir / f"{name}.json").write_text(
                json.dumps({"workflows": {wf.get("name") or name: redact(wf)}}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            print(f"[snapshot] {f.name} -> {name}.json")
            written += 1
    print(f"[snapshot] wrote {written} file(s) to {out_dir}")
    return 0 if written else 1


def self_test() -> int:
    t = "n8n-nodes-base.code"
    cond = "n8n-nodes-base.if"
    trig = "n8n-nodes-base.manualTrigger"
    bad = {
        "nodes": [{"name": "手动启动", "type": trig}, {"name": "[OUT] 汇总", "type": t},
                  {"name": "[MAIL-TPL] 渲染邮件模板", "type": t}, {"name": "[MAIL] 发邮件通知", "type": t}],
        "connections": {
            "手动启动": {"main": [[{"node": "[OUT] 汇总"}]]},
            "[OUT] 汇总": {"main": [[{"node": "[MAIL-TPL] 渲染邮件模板"}]]},
            "[MAIL-TPL] 渲染邮件模板": {"main": [[{"node": "[MAIL] 发邮件通知"}]]},
            "[MAIL] 发邮件通知": {"main": [[{"node": "[OUT] 汇总"}]]},
        },
    }
    good = {
        "nodes": [{"name": "手动启动", "type": trig}, {"name": "[MAIL] 发邮件通知", "type": t},
                  {"name": "[IF] 还有重试额度?", "type": cond}, {"name": "[CI] 触发 Jenkins 构建", "type": t}],
        "connections": {
            "手动启动": {"main": [[{"node": "[CI] 触发 Jenkins 构建"}]]},
            "[CI] 触发 Jenkins 构建": {"main": [[{"node": "[IF] 还有重试额度?"}]]},
            "[IF] 还有重试额度?": {"main": [[{"node": "[MAIL] 发邮件通知"}],
                                          [{"node": "[CI] 触发 Jenkins 构建"}]]},
        },
    }
    # Case-specific whitelists: the self-test must validate the CHECKER, not the
    # project's configuration (that is what `lint topology/` is for).
    bad_errors = lint_workflow("bad-cycle", bad, set(), EXPECTED_TERMINALS)
    good_errors = lint_workflow(
        "good-bounded-loop", good,
        {"[IF] 还有重试额度? -> [CI] 触发 Jenkins 构建"},
        {"[MAIL] 发邮件通知"},
    )
    for e in bad_errors:
        print("  bad  EXPECTED-ERROR " + e)
    print("bad  ->", "caught" if bad_errors else "GATE BROKEN (no error)")
    print("good ->", good_errors or "OK")
    ok = bool(bad_errors) and not good_errors
    print("self-test:", "PASS" if ok else "FAIL")
    return 0 if ok else 1


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("snapshot"); s.add_argument("--dump", required=True); s.add_argument("--out", required=True)
    l = sub.add_parser("lint"); l.add_argument("path")
    sub.add_parser("self-test")
    args = ap.parse_args()
    if args.cmd == "snapshot":
        return snapshot(Path(args.dump), Path(args.out))
    if args.cmd == "lint":
        return lint_dir(Path(args.path))
    return self_test()


if __name__ == "__main__":
    raise SystemExit(main())
