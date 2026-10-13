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
  "artifacts": { "rtt_log": "…", "stress_json": "…" },
  "source": "mcp|community",
  "fetch_ok": true,
  "http_status": 200,
  "notes": []
}
```

`[LOG]` is the **only** node allowed to know the fetch nodes' output shapes, so
swapping the fetch implementation is a change to `[LOG]` plus its upstream nodes
- no downstream node is touched. Three implementations exist or existed:

| Version | Fetch nodes | Status |
|---|---|---|
| Community | `[JENKINS] 取控制台日志` (`getBuildConsoleOutput`) + two `getBuildArtifact` + `extractFromFile` | **current main path**, `source='community'` |
| MCP | `[MCP] 取 Jenkins 控制台日志` (`getBuildLog`) | kept wired but **`disabled=true`** as a documented fallback |
| (first) HTTP | 3 authed `httpRequest` artifact fetches | deleted - the `jenkinsApi` predefined credential never applied auth (see *HTTP 200 is not success*) |

### Two bugs in the community node `n8n-nodes-jenkins` (v0.1.0)

Source: `dist/nodes/JenkinsNode/JenkinsNode.node.js:213, 218-224`.

```js
url = `${jenkinsBaseUrl}/job/${jobName}/${buildNumber}/artifact/${artifactPath}`;
response = await this.helpers.httpRequestWithAuthentication.call(this, 'jenkinsCredentialsApi', {method:'GET', url});
const binaryData = await this.helpers.prepareBinaryData(Buffer.from(response), artifactName);
newItem.binary[artifactName] = binaryData;
newItem.json[artifactName] = response;
```

1. **`artifactPath` must be the whole relative path** (`artifacts/rtt_stress.log`).
   `artifactName` is only the **output property** name, not a file name. Passing
   "directory + file" (`artifacts/` + `rtt_stress.log`) makes the node request
   `…/artifact/artifacts/` and it cheerfully returns the **artifact directory
   listing**: 185 kB of HTML handed over as if it were the artifact.
2. **`application/json` artifacts can never be fetched.**
   `httpRequestWithAuthentication` parses a JSON response into an object first,
   and `Buffer.from(object)` then throws. So `artifacts/person_search_stress.json`
   never comes back through `getBuildArtifact`, while text artifacts
   (`text/plain`, e.g. `rtt_stress.log`, 9 919 B) work.
3. Set `artifactName: 'data'`, otherwise the binary lands in a property named
   after the artifact and `extractFromFile` (which reads `data`) sees nothing.

Consequence for the stress numbers: the authoritative machine-readable summary
(`person_search_stress.json`: `iterations` / `passed` / `failed` / `pass_rate` /
`latency_sec` / `fail_reasons` / `records[]`) is **unreachable through this node
today**, so the console's `[stress] passed=N/total pass_rate=X% threshold=Y%`
line stays the effective source. It was cross-checked against the JSON on disk:
`passed:1, iterations:20, pass_rate:0.05` ⇔ `passed=1/20 pass_rate=5.00%`.
Per-iteration pass/fail is therefore derived from the console's own pytest lines
(`FAILED …::test_person_search_stress[N]` plus `====== 19 failed, 1 passed ======`),
which is why `records.source` can be `console_pytest`. `stress_source` and
`records.source` are the switch: once the node is fixed the JSON path takes over
with no downstream change.

### Context gate (P3)

Console logs and artifacts are large. Before anything reaches the model, the log
**and each artifact** passes a three-tier local gate (an artifact must not be able
to bypass P3 just because it arrived through a different node):

| Estimated tokens | Action |
|---|---|
| `> 8000` | **spill** - head/tail plus a locally filtered digest of the probe/`[stress]`/error lines, bounded by chars |
| `> 2000` | **prune** - head / middle / tail |
| otherwise | **pass** - verbatim |

Measured on the real build-255 run through the community node: `console_log`
184 823 chars → est_tok 46 206 → **spill**, 23 701 chars after gating, 217 digest
hits; `rtt_log` 9 919 chars → est_tok 2 480 → **prune**, 6 547 chars after gating,
182 digest hits. The digest mattered: with head/tail only the agent reported
*"the console contains only Jenkins pipeline scaffolding, no probe lines"*; with
the bounded digest of the matching lines it produced a real, evidence-cited
hypothesis (`[UART_ERR] code=0x00000002` plus truncated `[CAT1_RX]` fragments on
the UART4 DMA+IDLE receive path).

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

### `GR5526 HIL · 单 Agent TDD 流水线` (35 nodes / 38 edges, 10 groups)

Fetch version: **community** (`n8n-nodes-jenkins`, `source='community'`); the MCP
variant stays wired but `disabled=true` as a documented fallback.

Module map (n8n node groups):

| Group | Role |
|---|---|
| 01 编排 | task envelope from the form (`RUN_MODE` / `TARGET` / `MAX_RETRIES`) |
| 02 CI 构建与轮询 | trigger the build, poll every 30 s, normalize the build state |
| 03 证据采集（console + artifacts） | community console fetch + 2 artifact fetches → `extractFromFile` → `[LOG]` normalizes everything into the contract; also holds the disabled MCP fallback |
| 04 完整性门禁 | 6-state integrity gate: response body first, then status code |
| 05 机械分类与固件判定 | probe counting (console only) + stress from JSON/regex + `records[]`, and the FIRMWARE_FAIL gate |
| 06 上下文闸门与落盘 | P3 three-tier context gate (console **and** every artifact) + best-effort spill |
| 07 单 Agent 诊断 | frozen prefix, append-only history, single developer agent |
| 08 Token 账与重试计数 | per-round usage ledger, run totals, prefix invariant, retry counter |
| 09 升级文案 | escalation body |
| 10 交付（邮件） | the single terminal: summary -> HTML -> email |

Source-of-truth split (fixed by design):

| Data | Source | Used for |
|---|---|---|
| probe counts | console log | `[CLASSIFY]` mechanical classification |
| `rtt_log` | artifact | root-cause detail handed to the agent; **never counted** |
| `stress_json` | artifact | stress pass rate / `records[]` when reachable; console regex is the documented fallback |

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

### Known gap: `cat1_stress.log` is not fetched

* **What is missing is root-cause detail, not classification ability.** Probe
  counting (`downlink_recv` / `locasvr_reply` / `mqtt_send` / `cat1_ack` /
  `cat1_rx_events` / `uart_err`) comes 100% from the console log, verified
  field-by-field against the console path; `cat1_stress.log` (16 494 B) only adds
  Cat.1-side link detail.
* **How to add it**: one isomorphic node pair - `getBuildArtifact`
  (`artifactPath: 'artifacts/cat1_stress.log'`, `artifactName: 'data'`) plus
  `extractFromFile: text` - and one more key in `[LOG].artifacts`. The contract is
  an open map and `[GATE]` loops over its keys, so **no downstream node changes**.
* **When to add it**: when the break point lands on the Cat.1 side -
  `break_point ∈ {cat1_rx_at_stuck, reply_not_sent}` - or when `uart_err > 0` and
  the gated console digest is not enough to locate the fault.

## Installing a community node by hand on Windows

n8n's community-package installer shells out to `tar`, and on Windows the first
`tar` on `PATH` is normally a **GNU tar** - Git for Windows ships
`C:\Program Files\Git\usr\bin\tar.exe`, and on this machine it is msys64's
`D:\Program Files\msys64\usr\bin\tar.exe` (`tar (GNU tar) 1.35`). GNU tar reads a
leading `C:` as an **rsh-style remote host**, so an absolute Windows path fails:

```
tar: Cannot open: No such file or directory
```

It looks like the package refused to install, but the archiver is at fault, not
n8n. Workaround - fetch the tarball and unpack it with **Windows' own bsdtar**
(`%SystemRoot%\System32\tar.exe`):

```powershell
npm pack n8n-nodes-jenkins                        # -> n8n-nodes-jenkins-0.1.0.tgz
$dest = "$env:USERPROFILE\.n8n\.n8n\nodes\node_modules\n8n-nodes-jenkins"
New-Item -ItemType Directory -Force $dest
& "$env:SystemRoot\System32\tar.exe" -xzf .\n8n-nodes-jenkins-0.1.0.tgz -C $dest
Move-Item "$dest\package\*" $dest -Force          # flatten: package.json must sit at $dest
Remove-Item "$dest\package" -Recurse -Force
Restart-Service <your-n8n-service>                # node types are only read at boot
```

Notes:

* `<user folder>\nodes\node_modules\<package>` is the layout n8n loads. The
  tarball's top-level `package/` directory must be flattened so that
  `package.json` / `index.js` / `dist/` sit directly in `<package>`. On this
  machine the user folder is `C:\Users\Administrator\.n8n\.n8n` and the installed
  package reports `n8n-nodes-jenkins@0.1.0` (author Jabbson).
* Installed this way the package **does not appear under Settings → Community
  nodes**: that page reads the `installedPackageRepository` database table, not
  the directory. Nothing is broken - but there is no Uninstall button either, so
  removal means deleting the directory and restarting.
* A restart is required; a running n8n keeps the node types it already loaded.

## Known tooling limitations

Collected here because each of these looks like a bug in *this* workflow but is
not. All three were hit for real while building the pipeline.

### Node-level `notes` cannot be written through the n8n MCP surface

There is **no channel** to set a node's `notes` / `notesInFlow` field:

| Route | Result |
|---|---|
| `addNode` reusing an existing node's name | rejected - `a node named '…' already exists`; it neither merges nor replaces |
| `setNodeParameter` with JSON Pointer `/notes` | lands in **`parameters.notes`**; node-level `notes` stays `null` (the pointer is relative to `parameters`) |
| Workflow SDK `node()` | the SDK reference documents no `notes` field; the only route would be a full `create_workflow_from_code` rewrite |
| `setNodeSettings` | fixed schema (`onError` / `retryOnFail` / `maxTries` / …), no `notes` |

Node annotations are therefore carried by **sticky notes**, one per module,
listing what each node does as "input → action → output". Sticky notes are in the
linter's `SKIP_TYPES`, so they add no flow nodes and no edges.

### Do not use the Jenkins MCP for test results

The community node plus the console log already cover what `getTestResults` would
give:

* pytest prints failing-test **locals** to the terminal, so the full
  `stress_recorder` payload (`iteration` / `pass` / `latency_sec` / `reason`) is
  **already in the console log** - per-iteration pass/fail, failure reason and
  exact latency are all derivable from it. This pipeline does exactly that:
  `records.source = 'console_pytest'`, read from
  `FAILED …::test_person_search_stress[N]` plus the
  `====== 19 failed, 1 passed ======` summary line.
* `getBuild` / `getLastBuild` also return the junit action
  (`failCount` / `skipCount` / `totalCount`), available for cross-checking the
  console-derived counts.

Adding the MCP server for this would cost a dependency **and** an
`httpHeaderAuth` credential for zero extra information. The MCP node stays in the
workflow but `disabled=true`.

### A hand-unpacked community package has no UI entry

See *Installing a community node by hand on Windows*: Settings → Community nodes
reads the `installedPackageRepository` database table, it does not scan the nodes
directory. The package works, but it shows no entry and offers no Uninstall
button - removal means deleting the directory and restarting.

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
