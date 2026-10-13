# n8n workflow-as-code

Topology snapshots of the n8n workflows we own, plus a gate that refuses to
commit a broken graph.

## Why this exists

n8n **cannot detect an accidental cycle**. Loops are legal in n8n - a bounded
retry back-edge is a normal design - so the built-in validator only reports
local problems: missing parameters, disconnected nodes, node-group shape.

A leftover edge that closes a silent cycle has **no schema symptom at all**.
Observed in production: a chain already running `[OUT] -> [MAIL-TPL] -> [MAIL]`
kept a stale `[MAIL] -> [OUT]` edge from an earlier revision. Result: 11 duplicate
emails per run until QQ SMTP throttled the account with `550 Too many attempts`.

The runtime env vars (`EXECUTIONS_TIMEOUT`, `EXECUTIONS_TIMEOUT_MAX`) only bound
the damage; they do not find the bug. Only a graph check does.

## Rules the gate enforces

| Rule | Meaning |
|---|---|
| **R1** | Removing every whitelisted back-edge must leave an acyclic graph |
| **R1b** | Every whitelisted back-edge must actually exist in the graph |
| **R2** | Every remaining cycle must contain a conditional node that can exit it |
| **R3** | Nodes with no outgoing edge must match the declared terminal set |
| **R4** | Every node must be reachable from a trigger |
| **R5** | The same source/output/target edge must not appear twice |

R1 is deliberately formulated as *removal* rather than DFS back-edge discovery:
which edge DFS labels a "back edge" depends on traversal order, so the same
graph could pass or fail at random.

## Daily workflow

```bash
# 1. export from your n8n instance (LOCAL ONLY - never commit this dump)
n8n export:workflow --all --separate --output=dump/

# 2. reduce it to a redacted topology snapshot
python tools/n8n_topology.py snapshot --dump dump/ --out topology/

# 3. gate
python tools/n8n_topology.py self-test
python tools/n8n_topology.py lint topology/
```

## Never commit a raw export

A raw `n8n export` dump carries **API keys, Basic auth headers and parameter
values in clear text**. `snapshot` strips everything except node name/type and
wiring. Keep `dump/` gitignored.

## Adding a managed workflow

1. `snapshot` it into `topology/`.
2. Add its expected terminals and its whitelisted back-edges to the constants at
the top of `tools/n8n_topology.py`. That is a **design decision**, not a
formality: declaring a back-edge means answering "why must this loop terminate?"

## Current managed workflows

### `GR5526 TDD 双 Agent 流水线` (26 nodes / 28 edges)

Two bounded loops, both whitelisted:

| Back-edge | Bound |
|---|---|
| `[IF] 还在构建? -> [WAIT] 等 30 秒` | exits when Jenkins stops reporting `building=true` |
| `[IF] 还有重试额度? -> [CI] 触发 Jenkins 构建` | exits when `retry_count >= max_retries` (3) |

Terminal: `[MAIL] 发邮件通知` - the delivery email is the single sink.

## Adding a local hook (optional)

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: n8n-topology
        name: n8n topology lint
        entry: python tools/n8n_topology.py lint topology/
        language: system
        pass_filenames: false
```
