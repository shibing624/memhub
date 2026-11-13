<p align="center">
  <a href="https://github.com/shibing624/memhub">
    <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/logo.png" height="130" alt="memhub Logo" />
  </a>
</p>

<h1 align="center">memhub</h1>

<p align="center">
  <b>Local-first, versioned personal memory hub &amp; dual-system SDK for AI agents.</b><br />
  Share evolving facts, user profiles, and coding procedures across 18 AI tools (Claude Code, Codex, Cursor, Agentica, OpenClaw...) — getting sharper over time without conflicts.<br />
  <sub>100% open-source &amp; local-first · No account or telemetry · Zero-server Git sync · #1 on LoCoMo, LongMemEval &amp; PersonaMem-v2</sub>
</p>

<p align="center">
  <a href="https://badge.fury.io/py/memhub"><img src="https://badge.fury.io/py/memhub.svg" alt="PyPI version" /></a>
  <a href="https://github.com/shibing624/memhub/stargazers"><img src="https://img.shields.io/github/stars/shibing624/memhub?style=flat&color=yellow" alt="GitHub stars" /></a>
  <a href="https://github.com/shibing624/memhub/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License Apache 2.0" /></a>
  <a href="https://github.com/shibing624/memhub/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-green.svg" alt="Python 3.11+" /></a>
  <a href="https://github.com/shibing624/memhub/issues"><img src="https://img.shields.io/github/issues/shibing624/memhub.svg" alt="GitHub issues" /></a>
  <a href="#community--support"><img src="https://img.shields.io/badge/wechat-group-green.svg?logo=wechat" alt="Wechat Group" /></a>
</p>

<p align="center">
  <a href="https://github.com/shibing624/memhub/blob/main/README.md">简体中文</a> | English | <a href="https://shibing624.github.io/memhub">Documentation</a>
</p>

<p align="center">
  <a href="#60-second-quickstart">Quickstart</a> •
  <a href="#why-memhub">vs. claude-mem &amp; mem0</a> •
  <a href="#benchmarks-and-real-host-evaluation">Benchmarks</a> •
  <a href="#five-core-superpowers">Core Capabilities</a> •
  <a href="#for-developers">Python SDK / MCP</a>
</p>

https://github.com/user-attachments/assets/1fba5e03-219d-414e-8c96-8e63a11200fd

---

## Why memhub

> Not another append-only tool-activity logger — a local-first, version-evolving memory hub and SDK.

When working across coding agents and chats, the problem isn't "where to dump logs" — it's that most memory tools fail on four real things:

1. **Tool silos**: Project rules you settled on in Cursor don't follow you to Claude Code, Codex, or OpenClaw.
2. **Stale truths fighting new facts**: You told the assistant three times that you switched from scheme A to scheme B (or stopped eating spicy food). Append-only observation loggers keep **both old and new statements in context**, so the agent still trips over outdated advice.
3. **Activity logs instead of reusable debugging knowledge**: Dumping every `read_file` or `bash` call burns context tokens without distilling **"symptom → root cause → verified fix → touched files & branch"**.
4. **Lost history & vendor lock-in**: Many plugins only start recording after installation, throwing away hundreds of past sessions — or push you toward account sign-in, free-trial observers, and default telemetry.

**`memhub` is not a hook-based tool-activity logger — it is a local-first, version-evolving cognitive memory hub and standalone Python memory engine:**

| Dimension | `memhub` | `claude-mem` | `mem0` |
|---|---|---|---|
| **Core design** | **Versioned Memory Hub + Python SDK / REST / MCP** | Hook-driven tool activity logger (TS / Bun worker) | General conversation memory SDK & managed cloud |
| **Conflict & state evolution** | **Dedicated write-adjudication model (JEV)** picks `OVERRIDE` / `NEGATE` / `TEMPORAL` / `CONFLICT`, retiring old truths cleanly while preserving version history | Append-only observations & session summaries; old conclusions accumulate alongside new ones | LLM CRUD merge without explicit dual-layer conflict, validity window, or version chains |
| **Built for coding** | **Dedicated `procedure` memories** (symptom → fix → touched files + local file/branch drift check) + **live Project View** conventions | Stores tool-use observations & summaries; no structured procedures or drift verification | Plain-text memories; unaware of coding traces or git branches |
| **Token budget control** | **3,000-char budgeted progressive disclosure** + `[procedure-index]` folding + novel L0 delta excerpts + **retired-claim suppression** | 3-step MCP retrieval (`search` → `timeline` → `get_observations`) | Top-K chunk concatenation |
| **Past session import** | **`memhub scan` incrementally imports existing local transcripts across 18 tools** — smart on day 1 | Primarily captures new sessions after install (repo priming via `/learn-codebase`) | Requires custom ingestion scripts |
| **Multi-device sync & privacy** | **100% local-first, zero accounts, zero telemetry**; `connect git/dir/ssh` syncs **vector-bearing change logs** (0 model calls on receiver) | Cloud sync tied to cmem.ai; installer prompts sign-in / 30-day observer trial & default telemetry (opt-out) | Cross-device sharing relies on hosted cloud or self-hosted DB |
| **Head-to-head benchmarks** | **LoCoMo 74.7 (Temporal 78) · LongMemEval 91.7 · PersonaMem-v2 50.0 · 503s write** | No standard public memory benchmark reported | LoCoMo 65.6 (Temporal 22) · LongMemEval 83.3 · PersonaMem-v2 47.9 · 808s write |

---

## 60-Second Quickstart

### 1. Connect your installed AI tools (18 hosts supported)

```bash
pip install "memhub[all]"

# 1. Start the local background Core (starts at login, loopback 127.0.0.1 only; uninstall: memhub uninstall-service)
memhub install-service

# 2. Auto-detect and wire AI tools installed on this machine (keeps YAML comments; uninstall: memhub uninstall-host <tool>)
memhub install-host
# Or target specific hosts: memhub install-host claude-code / codex / cursor / agentica / ...
# Wire all 18 at once: memhub install-host all

# 3. (Optional) Import your existing local conversation history into memory
memhub scan --source all

# 4. Recall from the CLI or browse/edit visually in your browser
memhub recall "what technical choices have I made before"
memhub ui                          # opens http://127.0.0.1:8765/ui
```

<p align="center">
  <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/ui-mem.png" width="840" alt="memhub ui memory browser" />
</p>

Once connected, everything runs hands-free: when a turn finishes, new evidence is organized asynchronously into versioned memories; on your next prompt in any supported tool, local project rules, user profile, relevant facts, and engineering procedures are injected in tens of milliseconds (and agents can query/update proactively via MCP tools `memory_search`, `memory_get`, `conversation_search`, `memory_update`).

<details>
<summary>Supported 18 AI tools (19 entry points) and integration matrix</summary>

| Host tool | Prompt-time injection | Auto capture | MCP tools | History import (`memhub scan`) |
|---|:---:|:---:|:---:|:---:|
| **Claude Code** (`claude-code`) | ✅ | ✅ | ✅ | ✅ |
| **Codex** (`codex`) | ✅ | ✅ | ✅ | ✅ |
| **Cursor** (`cursor`) | ✅ | ✅ | ✅ | ✅ |
| **Agentica** (CLI / Web / Desktop / IM) | ✅ | ✅ | ✅ | ✅ |
| **OpenCode** (`opencode`) | ✅ | ✅ | ✅ | ✅ |
| **CodeBuddy** (`codebuddy`) | ✅ | ✅ | ✅ | ✅ |
| **WorkBuddy** (`workbuddy`) | ✅ | ✅ | ✅ | ✅ |
| **Kimi Code** (`kimi-code`) | ✅ | ✅ | ✅ | ✅ |
| **Kimi CLI** (`kimi-cli`) | MCP + Skill | ✅ | ✅ | ✅ |
| **Trae** (`trae`) | ✅ | ✅ | ✅ | — |
| **ZCode** (`zcode`) | ✅ | ✅ | ✅ | ✅ |
| **Qoder** (`qoder`) | ✅ | ✅ | ✅ | ✅ |
| **GitHub Copilot** (`copilot`) | ✅ | ✅ | ✅ | ✅ |
| **AWS Kiro** (`kiro`) | ✅ | ✅ | ✅ | ✅ |
| **DeepSeek Harness** (`dsh`) | MCP + Skill | `scan` | ✅ | ✅ |
| **Hermes** (`hermes`) | MCP + Skill | `scan` | ✅ | ✅ |
| **Cline** (`cline`) | MCP + Skill | `scan` | ✅ | ✅ |
| **Pi** (`pi`) | MCP + Skill | `scan` | ✅ | ✅ |
| **OpenClaw** (`openclaw`) | Skill + CLI | `scan` | — | ✅ |

</details>

### 2. Zero-server sync across computers (no re-embedding)

Every computer keeps a full local store and **always recalls locally (works offline)**. Machines exchange float16-compressed vector change logs and tombstones — **the receiving machine makes zero LLM or embedding calls**:

```bash
# Option 1 (Recommended): Sync & back up via a private Git repo (each machine writes only its own origin dir — zero merge conflicts)
memhub connect git git@github.com:you/my-memory.git

# Option 2: Sync via a cloud folder (iCloud Drive / Dropbox / OneDrive / Syncthing)
memhub connect dir "~/Library/Mobile Documents/com~apple~CloudDocs/memhub"

# Option 3: Connect to your own server
memhub connect ssh user@your-server
```

See the [Memory Hub docs](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) and [Multi-device sync guide](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md).

---

## Benchmarks and Real Host Evaluation

### 1. Head-to-head public benchmarks (same LLM, same embeddings)

Extraction, answering, and grading all use **Qwen3.8-27B** and **bge-m3** from an empty store — **only the memory system changes**:

| Memory system | LoCoMo<br>(308 q) | LoCoMo Temporal<br>(time reasoning) | LongMemEval<br>(24 q) | PersonaMem-v2<br>(476 q) | Ingestion time per long dialogue<br>(s, lower is better) |
|---|:---:|:---:|:---:|:---:|:---:|
| **memhub** | **74.7** | **78** | **91.7** | **50.0** | **503** |
| hy-memory 2.0.4 | 66.6 | 61 | 70.8 | 43.5 | 2485 |
| mem0 | 65.6 | 22 | 83.3 | 47.9 | 808 |

- **#1 across all three public benchmarks**: +8–9 points overall on LoCoMo, and **78 vs 61 / 22** on temporal reasoning.
- **~5x faster writes**: median per-dialogue ingestion time is ~1/5 of hy-memory and 38% faster than mem0.
- Full protocol, statistical tests, and reproduction scripts: [Benchmarks](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md).

### 2. End-to-end check on 5 real coding CLIs

With project-only conventions stored strictly inside an isolated Hub Core ([reproduction & details](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md)):

| Coding CLI | Native (without memhub) | With memhub |
|---|:---:|:---:|
| Claude Code | 0/5 | **5/5** |
| Codex | 0/5 | **5/5** |
| OpenCode | 0/5 | **5/5** |
| kimi-cli | 0/5 | **5/5** |
| kimi-code | 0/5 | **5/5** |

---

## Five Core Superpowers

### 1. 🧠 Fast & Slow Dual Systems + JEV Write Adjudication
- **Four canonical layers**: raw evidence (`L0_RAW`) → explicit facts / events / intentions / engineering procedures (`L1_FACT`) → user profile & current state (`L2_PROFILE`) → cross-evidence schemas & persona catalog (`L3_DERIVED`).
- **Dedicated JEV adjudicator**: Instead of blindly appending, a lightweight decision model compares candidates against existing heads and assigns `ADD_OVERRIDE`, `ADD_SUPPLEMENT`, `ADD_TEMPORAL` (bounded validity windows), `ADD_NEGATE`, `ADD_CONFLICT`, `ADD_HISTORICAL` (out-of-order logs never overwrite newer truths), `SKIP`, or `DELETE`.
- **System 2 background induction (`dual` mode)**: Clusters accumulated facts across sessions to surface long-term habits and working style.

### 2. 🛠️ Purpose-built for Coding: Procedures, Live Project View & Drift Checks
- **Automatic `procedure` distillation**: Uses `tool_calls` and `exit_code` execution signals (while leaving bulky tool output out of storage) to capture "task → root cause & fix → touched files → branch/env boundary", automatically filtering non-human automated/cron turns.
- **Query-independent Project View**: Even if you only ask "add a unit test", critical project guardrails ("never touch prod.db", "freeze window rules") are surfaced alongside dynamic recall so rules aren't missed due to phrasing.
- **Live local drift hints**: Alerts the agent if files cited by an older procedure were deleted/renamed locally or belong to a different git branch.

### 3. 📉 Budgeted Progressive Disclosure + Retired-Claim Suppression
- Prompt injection respects a **3,000-char / 120-line budget**: high-scoring procedures expand in full while others fold into single-line `[procedure-index id=...]` entries; same-source `L0_RAW` excerpts drop already-covered or retired statements and keep only novel context.
- Agents pull full procedures or surrounding original turns on demand via `memory_get(id)` and `conversation_search(source_id=...)`.

### 4. ⚡ Fast Local Hybrid Recall + Git Worktree Alignment
- Combines Chroma vector recall with a shared inverted BM25 index (`LexicalIndex`) and RRF fusion (~0.17s steady-state recall on 5,600+ local memories).
- Automatically resolves git main repo roots, linked `git worktree`s, submodules, and `origin` `org/repo` slugs — unifying memories across parallel worktrees and machines with different clone paths without leaking same-named private repos.

### 5. 🔒 Strict Privacy & Self-Healing Queue
- **`<private>...</private>` tags**: Strip sensitive spans inline while keeping the rest of the turn intact.
- **Base64 image elision & optional secret redaction**: Automatically strips inline screenshots; enable `redact_secrets: true` to redact 20+ credential formats before writing to disk.
- **Visual browser (`memhub ui`) & `memhub doctor`**: Browse by project, source tool, or session ID, edit (auto re-embeds & syncs), or archive memories; backed by durable retry queues and health warnings.

---

## For Developers

Embed `memhub` directly into your own agents, RAG pipelines, or services (Python SDK / Agent Frameworks / MCP / REST):

```bash
pip install memhub
```

```python
from memhub import SyncMemoryClient

with SyncMemoryClient() as memory:
    memory.add(content="I live in Shanghai and recently stopped eating spicy food.", user_id="alice")
    res = memory.search(query="recommend a restaurant for alice", user_id="alice")
```

- **Python SDK Quickstart** (sync/async `MemoryClient`, coding procedures, point-in-time `as_of` queries): [Quickstart](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md)
- **Agent Framework Integrations** (Agentica `MemHubWorkspace`, Claude Code hooks, Hermes, OpenClaw): [Integrations](https://github.com/shibing624/memhub/blob/main/docs/integrations.md)
- **MCP & Multi-Tenant REST Server** (`memhub-mcp` / `memhub serve-api`): [MCP / REST](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md)
- **Runnable End-to-End Examples**: [`examples/`](https://github.com/shibing624/memhub/tree/main/examples)

## Install & Configure

```bash
pip install "memhub[all]"
```

Edit `~/.memhub/config.yaml` (created automatically on first run) and fill in your API keys (works with any OpenAI-compatible gateway or local model):

```yaml
jev_api_key: ""    # decision model key for write adjudication
jev_endpoint: https://api.typesafe.ai/v1/systemone

llm_api_key: ""    # LLM key for extraction and System 2 (OpenAI-compatible)
llm_base_url: https://api.openai.com/v1

embed_api_key: ""  # embedding key for vector indexing and search (OpenAI-compatible)
embed_base_url: https://api.openai.com/v1
```

> For environment variables and advanced settings, see [Installation & Configuration](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md).

## Roadmap

- [x] System 1 layered memory and version evolution (`OVERRIDE` / `NEGATE` / `TEMPORAL` / `CONFLICT`)
- [x] System 2: cross-evidence pattern induction, profile catalog (L3), and Kuzu evidence graph projection
- [x] Dedicated decision model guarding writes
- [x] Memory Hub: 18 AI tools share memory, history import, background organizing
- [x] Multi-device sync: local recall on every computer, works offline, exchanges changes via a private Git repo / synced folder / server
- [x] Automatic engineering experience distillation, budgeted progressive disclosure, and live project-convention view
- [x] Leads head-to-head on LoCoMo / LongMemEval / PersonaMem-v2; 5/5 across 5 coding CLIs (vs. 0/5 native)
- [x] Visual memory management: `memhub ui` to browse by project and source tool, search, edit, and delete memories
- [ ] Cloud HTTP MCP: remote agents connect to hosted memory directly
- [ ] Better retrieval for multi-hop questions (LoCoMo multi-hop still has headroom)
- [ ] More public benchmarks (CodeMemoryBench and others, once their data is released)

Vote or request features in [Issues](https://github.com/shibing624/memhub/issues).

## News

- [2026/11/12] **v0.2.4**: Host support now covers 18 tools (Kimi, DeepSeek Harness, Trae, ZCode, Qoder, GitHub Copilot, AWS Kiro, Cline, Pi, and OpenClaw). `memhub install-host` with no arguments only wires tools already installed on this machine. Added `memhub doctor`, `memory_get` / `memhub memory-get`, budgeted progressive disclosure of procedures, and a live project-convention view that does not depend on query wording. Agentica Web, Desktop, and IM are captured and recalled like the CLI; pure SDK sessions are excluded. `/ui` filters by source tool, and the search box matches source, session id, and short memory id. Re-run `memhub install-host` on hosts already installed. See [Release-v0.2.4](https://github.com/shibing624/memhub/releases/tag/v0.2.4).
- [2026/11/03] **v0.2.3**: Cursor sessions are captured automatically and recalled into the prompt before each question. `memhub remember` writes a personal memory. The host skill (`memhub-recall`) and the developer skill (`memhub-sdk`) no longer share a name. `memhub ui` uses a light theme and no longer jumps to the top or breaks IME composition. `versions.db` no longer stores a second copy of vectors. Re-run `memhub install-host` on hosts already installed. See [Release-v0.2.3](https://github.com/shibing624/memhub/releases/tag/v0.2.3).
- [2026/11/02] **v0.2.2**: Rebranded the repository and PyPI package to **`memhub`** (`pip install memhub`, `import memhub`, CLI `memhub`, config `~/.memhub/config.yaml`), promoting all Hub commands to top-level `memhub` subcommands. Added the local memory browser `memhub ui`; added CodeBuddy, OpenCode and WorkBuddy host integrations; bounded sync targets with float16 vectors and snapshot compaction; scoped prompt-time recall to the current project and relevant dialogue passages (35/36 on the offline coding eval). See [Release-v0.2.2](https://github.com/shibing624/memhub/releases/tag/v0.2.2).
- [2026/11/01] **v0.2.1**: Every computer keeps a full local memory and recalls locally. Changes sync through a private git repo, a synced folder, or a server; the always-on SSH tunnel is gone. Model config and Hub home live in the same directory. Coding memory keeps the question and the final answer; tool output is no longer sent to a model. Auto digest runs at most once a day. See [Release-v0.2.1](https://github.com/shibing624/memhub/releases/tag/v0.2.1).
- [2026/10/24] **v0.2.0**: Chat and coding now share one `add()` / `search()`. An engineering session with a tool trace yields `kind="procedure"` experience next to its chat memories, on the same store, version chain and JEV adjudication; `dual` `digest()` covers them too. Captures tool traces from Claude Code / Codex / Agentica and auto-digests when idle in dual mode; `install-host` keeps YAML comments. See [Release-v0.2.0](https://github.com/shibing624/memhub/releases/tag/v0.2.0).

<details>
<summary>Earlier releases</summary>

- [2026/10/23] **v0.1.5**: Fixed Coding memory writes against real models. Extraction now returns a `{"candidates": [...]}` object; the coverage hard gate is gone; one batch can supersede the same head; JEV only sees heads inside the draft's version boundary; concurrent writes for one user serialize per scope. See [Release-v0.1.5](https://github.com/shibing624/memhub/releases/tag/v0.1.5).
- [2026/10/23] **v0.1.4**: Added local single-Core cross-tool memory hub (incremental session import for Claude Code / Codex / Cursor / Hermes / Agentica, durable background write queue, login service); upgraded System 2 v3 clustered cognition and version-bridge recall; ranked #1 head-to-head on LoCoMo (74.7), LongMemEval (91.7), and PersonaMem-v2 (50.0). See [Release-v0.1.4](https://github.com/shibing624/memhub/releases/tag/v0.1.4).
- [2026/10/15] **v0.1.3**: Redesigned around four canonical layers and target-first JEV write decisions with linked version history; added native integrations for Agentica / Claude Code / Hermes / OpenClaw and the Coding memory subsystem. See [Release-v0.1.3](https://github.com/shibing624/memhub/releases/tag/v0.1.3).
- [2026/06/20] **v0.1.2**: Added `SyncMemoryClient` synchronous wrapper, automatic default config bootstrap on first run, unified REST / MCP / CLI contracts, and the online documentation site. See [Release-v0.1.2](https://github.com/shibing624/memhub/releases/tag/v0.1.2).
- [2026/06/19] **v0.1.1**: Released dual-system layered memory SDK with hybrid retrieval (semantic + BM25 + RRF) and CLI / REST / MCP interfaces. See [Release-v0.1.1](https://github.com/shibing624/memhub/releases/tag/v0.1.1).
- [2026/06/18] **v0.1.0**: First open-source release.

</details>

Full history in the [CHANGELOG](https://github.com/shibing624/memhub/blob/main/CHANGELOG.md).

## Docs

Full documentation: **[https://shibing624.github.io/memhub](https://shibing624.github.io/memhub)**

| Doc | Contents |
|---|---|
| [Memory Hub](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) | 18 host integrations, history import, `memhub ui` visual browser |
| [Multi-device sync](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md) | private `git` repo / synced `dir` / `ssh` / `http` replication, local recall |
| [Installation & configuration](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md) | extras, config file, environment variables and precedence |
| [SDK Quickstart](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md) | sync / async Python clients, coding memory, multi-turn writes |
| [Integrations](https://github.com/shibing624/memhub/blob/main/docs/integrations.md) | Agentica / Claude Code / Hermes / OpenClaw |
| [MCP / REST](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md) | MCP tools and HTTP REST mapping |
| [Benchmarks](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md) | protocol, question sets, reproduction, breakdowns |
| [Host tools check](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md) | native vs +memhub check across 5 coding CLIs (in Chinese) |
| [Examples](https://github.com/shibing624/memhub/blob/main/examples/README.md) | 6 runnable end-to-end example scripts |
| [Architecture & Design](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) | optional: [Architecture](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) · [Memory layers](https://github.com/shibing624/memhub/blob/main/docs/memory_layers.md) · [Write decisions](https://github.com/shibing624/memhub/blob/main/docs/jev-memory-design.md) |

## Community & support

> If `memhub` helps, a ⭐ star helps more developers find it!

- **GitHub Issues** — [open an issue](https://github.com/shibing624/memhub/issues)
- **WeChat group** — add `xuming624`, note "llm"

<img src="https://github.com/shibing624/agentica/blob/main/docs/assets/wechat.jpeg" width="200" alt="WeChat group QR" />

## Citation

If you use memhub in research, please cite:

> Xu, M. (2026). memhub: Personal Memory Hub and Dual-System Layered Memory SDK for AI Agents. GitHub. https://github.com/shibing624/memhub

```bibtex
@misc{xu2026memhub,
  author    = {Xu, Ming},
  title     = {memhub: Personal Memory Hub and Dual-System Layered Memory SDK for AI Agents},
  year      = {2026},
  publisher = {GitHub},
  url       = {https://github.com/shibing624/memhub}
}
```

## License

[Apache License 2.0](https://github.com/shibing624/memhub/blob/main/LICENSE)

## Contributing

Contributions welcome. See [CONTRIBUTING.md](https://github.com/shibing624/memhub/blob/main/CONTRIBUTING.md).

## Acknowledgements

- [Chroma](https://github.com/chroma-core/chroma)
- [Kuzu](https://github.com/kuzudb/kuzu)
- [FastAPI](https://github.com/tiangolo/fastapi)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [mem0](https://github.com/mem0ai/mem0)
