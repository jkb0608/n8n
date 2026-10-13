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

The snapshot is rendered compactly (one line per node and per connection source)
and written with LF endings, so a diff shows a topology change rather than a
formatter or line-ending artefact. A CRLF working copy differs from the committed
file by exactly one byte per line - that is not a content change.

## Never commit a raw export

A raw `n8n export` dump carries **API keys, Basic auth headers and parameter
values in clear text**. `snapshot` strips everything except node name/type,
wiring, the whitelisted settings and the node-group map. Keep `dump/` gitignored.

## Failure isolation: HTTP 200 is not success

Failure isolation is a *design* rule here, not a coding style: **no evidence is
not the same as a failing test, and a failing test is not the same as a firmware
fault.** Inferring a firmware root cause from a transport error produces a
confident, wrong conclusion, which is worse than no conclusion.

Two live examples from this repository's own pipeline:

1. Three `[FETCH]` HTTP nodes used `authentication: predefinedCredentialType`
   with the `jenkinsApi` credential. The credential works on the Jenkins node,
   but the HTTP Request node sent **no Authorization header at all** - Jenkins
   answered `403` with an `Authentication required` HTML page.
   `neverError: true` swallowed it, the classifier counted probes in the HTML
   error page and emitted `break_point: downlink_lost` - a fabricated firmware
   finding.
2. The Jenkins MCP server accepts the MCP handshake **anonymously**, and
   `tools/call getBuildLog` also answers **HTTP 200** - but the body is
   `result.isError = true` with
   `AccessDeniedException3: anonymous is missing the Overall/Read permission`.

So a guard that only inspects the status code is not a guard. The rule the
pipeline now enforces:

> Judge **the response body first** (`isError`, an `error` field, `status:
> FAILED`, an HTML login page, an empty body), and only then the status code.

## Replaceable fetch boundary

The diagnostic half of the pipeline (integrity guard, classification, context
gate, prompt, agent) must not depend on *how* the console log was fetched.
Everything downstream consumes one normalized contract, produced by a single
node (`[LOG] 规范化日志契约`):

```json
{
  "console_log": "…",
  "artifacts": { "rtt": "…", "cat1": "…" },
  "source": "mcp|community",
  "fetch_ok": true,
  "http_status": 200,
  "notes": []
}
```

Swapping the fetch implementation (Jenkins MCP client today, the community
`n8n-nodes-jenkins` package's *Get Build Console Output* / *Get Build Artifact*
later) therefore changes exactly one upstream node; no downstream node has to be
touched.

### Context gate (P3)

Console logs are large. Before anything reaches the model, the log passes a
three-tier local gate:

| Estimated tokens | Action |
|---|---|
| `> 8000` | **spill** - head/tail plus a locally filtered digest of the probe/`[stress]`/error lines, bounded by chars |
| `> 2000` | **prune** - head / middle / tail |
| otherwise | **pass** - verbatim |

Measured on the real build-255 console log (216 919 chars): the digest mattered.
With head/tail only, the agent reported *"the console contains only Jenkins
pipeline scaffolding, no probe lines"*; with the bounded digest of the 217
matching lines it produced a real, evidence-cited hypothesis
(`[UART_ERR] code=0x00000002` plus truncated `[CAT1_RX]` fragments on the UART4
DMA+IDLE receive path).

### Known limitation: spill does not actually reach disk here

The spill branch ends in `readWriteFile`, and on this instance that node is
refused by n8n's file-access policy:

```
Access to the file is not allowed.
```

n8n only permits the file node inside its files directory
(`~/.n8n-files`, which does not exist on this host), and it does not create
parent directories. To make spill a real disk artifact you would need either that
directory to exist or `N8N_BLOCK_FILE_ACCESS_TO_N8N_FILES=false`.

Until then, **spill is effectively a strong prune**: the file is never written,
and the agent has no tool that could re-read it anyway (there is no file-reading
tool attached to the agent). The email reports the write outcome (`落盘写入`)
instead of silently claiming a file. The Jenkins console log itself remains the
durable source of record.

## Test double

`tools/mcp_testdouble/mock_jenkins_mcp.py` is a **test-only** minimal MCP
server (initialize / tools/list / tools/call) that replays the *real* console log
of a Jenkins build. It exists because the authorized `getBuildLog` path needs an
`httpHeaderAuth` credential that is not set up yet, while the blocked path alone
cannot exercise classification, the context gate or the token ledger.

```bash
python tools/mcp_testdouble/mock_jenkins_mcp.py 8899      # high port only
# point the workflow's MCP node at http://127.0.0.1:8899/mcp, run, then restore
# the endpoint to http://localhost:8080/mcp-server/mcp and VERIFY the restore by
# re-exporting the workflow and reading the value back.
```

Never bind 8080 (Jenkins) or 5678 (n8n). Stop it when done: cancelling a job
wrapper can orphan the child process, so confirm with
`Get-NetTCPConnection -LocalPort 8899 -State Listen`.

## Adding a managed workflow

1. `snapshot` it into `topology/`.
2. Add its expected terminals and its whitelisted back-edges to the constants at
   the top of `tools/n8n_topology.py`. That is a **design decision**, not a
   formality: declaring a back-edge means answering "why must this loop terminate?"

## Current managed workflows

### `GR5526 HIL · 单 Agent TDD 流水线` (29 nodes / 33 edges)

Module map (n8n node groups):

| Group | Role |
|---|---|
| 01 编排 | task envelope from the form (`RUN_MODE` / `TARGET` / `MAX_RETRIES`) |
| 02 CI 构建与轮询 | trigger the build, poll every 30 s, normalize the build state |
| 03 证据采集与完整性门禁 | fetch the console log, normalize it to the contract, 6-state integrity gate |
| 04 机械分类与固件判定 | probe counting + `[stress]` parsing, and the FIRMWARE_FAIL gate |
| 05 上下文闸门与落盘 | P3 three-tier context gate + best-effort spill |
| 06 单 Agent 诊断 | frozen prefix, append-only history, single developer agent |
| 07 Token 账与重试计数 | per-round usage ledger, prefix invariant, retry counter |
| 08 升级文案 | escalation body |
| 09 交付（邮件） | the single terminal: summary -> HTML -> email |

Two bounded loops, both whitelisted:

| Back-edge | Bound |
|---|---|
| `[IF] 还在构建? -> [WAIT] 等 30 秒` | exits when Jenkins stops reporting `building=true` |
| `[IF] 还有重试额度? -> [CI] 触发 Jenkins 构建` | exits when `retry_count >= max_retries` (3) |

Terminal: `[MAIL] 发邮件通知` - the delivery email is the single sink, and it is
reached on all three outcomes (build passed / evidence blocked / not fixed).
Only `FIRMWARE_FAIL` may enter firmware root-cause analysis and the retry loop.
Either gate failing routes straight to the summary with **zero LLM calls**.

State model: `PASS` / `FIRMWARE_FAIL` / `DATA_UNAVAILABLE` / `API_ERROR` /
`TIMEOUT` / `INCONCLUSIVE`.

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
