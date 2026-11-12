<p align="center">
  <a href="https://github.com/shibing624/memhub">
    <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/logo.png" height="130" alt="memhub Logo" />
  </a>
</p>

<h1 align="center">memhub</h1>

<p align="center"><b>One memory across your agents. Facts now, patterns over time.</b><br />Claude Code, Codex, Cursor, Agentica and 14 other AI tools share one local memory with cross-device sync and a visual browser.</p>

<p align="center">
  <a href="https://badge.fury.io/py/memhub"><img src="https://badge.fury.io/py/memhub.svg" alt="PyPI version" /></a>
  <a href="https://github.com/shibing624/memhub/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License Apache 2.0" /></a>
  <a href="https://github.com/shibing624/memhub/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-green.svg" alt="Python 3.11+" /></a>
  <a href="https://github.com/shibing624/memhub/issues"><img src="https://img.shields.io/github/issues/shibing624/memhub.svg" alt="GitHub issues" /></a>
  <a href="#community--support"><img src="https://img.shields.io/badge/wechat-group-green.svg?logo=wechat" alt="Wechat Group" /></a>
</p>

<p align="center"><a href="https://github.com/shibing624/memhub/blob/main/README.md">简体中文</a> | English | <a href="https://shibing624.github.io/memhub">Documentation</a></p>

https://github.com/user-attachments/assets/1fba5e03-219d-414e-8c96-8e63a11200fd

## Why memhub

The coding conventions you explained in Cursor are unknown to Codex. You've told your assistant three times that you no longer eat spicy food, and it still recommends a Sichuan restaurant. After dozens of turns it can quote what you said, and still cannot name the habit that shows up every time.

### 1. 🔗 Several agents, one memory

`memhub` connects the AI tools on your machine to one personal memory: **Claude Code, Codex, Cursor, Agentica, Hermes, CodeBuddy, OpenCode, WorkBuddy, Kimi (kimi-cli / kimi-code), DeepSeek Harness, Trae, ZCode, Qoder, GitHub Copilot, AWS Kiro, Cline, Pi, and OpenClaw** read and write the same local store. By default the data stays on your computer.

```bash
pip install "memhub[all]"

# 1. Start the background service (starts at login; remove with memhub uninstall-service)
memhub install-service

# 2. Connect the AI tools you use (remove with memhub uninstall-host <tool>)
#    With no argument it detects the tools installed on this machine:
memhub install-host
memhub install-host claude-code
memhub install-host codex
memhub install-host cursor
# Supported: claude-code | codex | cursor | agentica | hermes | codebuddy | opencode | workbuddy | kimi-cli | kimi-code | dsh | trae | zcode | qoder | copilot | kiro | cline | pi | openclaw
# Or connect all at once: memhub install-host all

# 3. (Optional) Import your past conversation history into memory
memhub scan --source claude-code   # or --source all to import from all installed tools

# 4. Recall from the CLI or manage memories visually in your browser
memhub recall "what technical choices have I made before"
memhub ui                          # open http://127.0.0.1:8765/ui to browse, search, edit and delete memories by project
```

<p align="center">
  <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/ui-mem.png" width="820" alt="memhub ui memory browser" />
</p>

Once connected, it works automatically: when a turn ends, new messages are organized in the background; when you type your next prompt, relevant project-scoped memories are injected automatically, and agents can also query through MCP.

**Share one memory across computers**: every computer keeps a full local copy and recalls from it locally (even offline); changes and vectors sync in the background without calling any model on the receiving side. Pick one of three ways:

```bash
# Option 1 (Recommended): Sync & back up via a private Git repository
memhub connect git git@github.com:you/my-memory.git

# Option 2: Sync via a cloud folder (iCloud Drive / Dropbox / OneDrive / Syncthing)
memhub connect dir "~/Library/Mobile Documents/com~apple~CloudDocs/memhub"

# Option 3: Connect to your own server
memhub connect ssh user@your-server
```

See the [Memory Hub docs](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) and [multi-device sync guide](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md).

### 2. 🏆 Same model, sharper memory, faster writes

One model (Qwen3.8-27B) does extraction, answering and grading. One embedding model (bge-m3). The same questions. **Only the memory system changes**:

| Memory system | LoCoMo<br>(308 q) | LongMemEval<br>(24 q) | PersonaMem-v2<br>(476 q) | Ingestion time per long dialogue<br>(s, lower is better) |
|---|:---:|:---:|:---:|:---:|
| **memhub** | **74.7** | **91.7** | **50.0** | **503** |
| hy-memory 2.0.4 | 66.6 | 70.8 | 43.5 | 2485 |
| mem0 | 65.6 | 83.3 | 47.9 | 808 |

**First on all three public benchmarks, and about one fifth the ingestion time of hy-memory.** Protocol, significance tests, per-category results and reproduction commands: [Benchmarks](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md).

### 3. 🧠 Fast & slow dual systems: facts now, patterns over time

- **Fast system (System 1)** remembers your facts, preferences and state changes in real time. When you change your mind, move cities, or update a technical plan, it appends a new version and keeps the history — answers always use the latest state.
- **Slow system (System 2)** induces your long-term habits and working style across conversations in the background, getting to know you better over time.

### 4. 🛠️ Debugging sessions stay useful

After fixing a tricky bug or settling on a project convention, you don't need to take notes by hand. Coding sessions automatically distill "problem → root cause & solution → touched files" into reusable engineering experience, bringing back proven fixes the next time a similar issue comes up.

## Install & Configure

```bash
pip install "memhub[all]"
```

Edit `~/.memhub/config.yaml` (created automatically on first run) and fill in your API keys:

```yaml
jev_api_key: ""    # decision model key for write adjudication
jev_endpoint: https://api.typesafe.ai/v1/systemone

llm_api_key: ""    # LLM key for extraction and System 2 (OpenAI-compatible)
llm_base_url: https://api.openai.com/v1

embed_api_key: ""  # embedding key for vector indexing and search (OpenAI-compatible)
embed_base_url: https://api.openai.com/v1
```

> For environment variables and advanced settings, see [Installation & Configuration](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md).

## For Developers (Python SDK / Agent Frameworks / MCP / REST)

Building your own agent or application with `memhub`? Check out the developer guides:

- **Python SDK Quickstart** (everyday memory, coding memory, sync & async clients): [Quickstart](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md)
- **Agent Framework Integrations** (Agentica, Claude Code hooks, Hermes, OpenClaw): [Integrations](https://github.com/shibing624/memhub/blob/main/docs/integrations.md)
- **MCP & REST API Server**: [MCP / REST](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md)
- **Runnable End-to-End Examples**: [`examples/`](https://github.com/shibing624/memhub/tree/main/examples)

## Features

- **Cross-tool sharing & multi-device sync** — 18 AI tools share one local memory, synced across machines via a private Git repo, cloud folder, or server
- **Visual memory browser** — `memhub ui` to filter by project and type, run semantic search, edit (re-embeds and syncs automatically), and delete memories
- **Versioned updates** — "lives in Shanghai → moving to Beijing" updates the current version while keeping history
- **Layered memory** — raw conversations (L0), facts & engineering procedures (L1), user profile (L2), and cross-evidence patterns (L3)
- **Decision model guarding writes** — a dedicated lightweight model (JEV) adjudicates whether incoming information is new, duplicate, or an update

## Roadmap

- [x] System 1 layered memory and version evolution
- [x] System 2: cross-evidence pattern induction and profile catalog (L3)
- [x] Dedicated decision model guarding writes
- [x] Memory Hub: 18 AI tools (Claude Code / Codex / Cursor / Agentica / Hermes / CodeBuddy / OpenCode / WorkBuddy / Kimi / DeepSeek Harness / Trae / ZCode / Qoder / Copilot / Kiro / Cline / Pi / OpenClaw) share memory, history import, background organizing
- [x] Multi-device sync: local recall on every computer, works offline, exchanges changes via a private Git repo / synced folder / server
- [x] Automatic engineering experience distillation and project-scoped recall
- [x] Leads head-to-head on LoCoMo / LongMemEval / PersonaMem-v2
- [x] Visual memory management: `memhub ui` to browse by project, search, edit and delete memories
- [x] More host tools: DeepSeek Harness, Trae, ZCode, Qoder, Copilot, Kiro, Cline, Pi, OpenClaw and others — 18 in total, with a [measured native vs. memhub check](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md) (documented in Chinese)
- [ ] Cloud HTTP MCP: remote agents connect to hosted memory directly
- [ ] Better retrieval for multi-hop questions (LoCoMo multi-hop still has headroom)
- [ ] More public benchmarks (CodeMemoryBench and others, once their data is released)

Vote or request features in [Issues](https://github.com/shibing624/memhub/issues).

## News

- [2026/11/12] **v0.2.4**: Host support now covers 18 tools (Kimi, DeepSeek Harness, Trae, ZCode, Qoder, GitHub Copilot, AWS Kiro, Cline, Pi, and OpenClaw). `memhub install-host` with no arguments only wires tools already installed on this machine. Added `memhub doctor`, `memory_get` / `memhub memory-get`, budgeted progressive disclosure of procedures, and a live project-convention view that does not depend on query wording. Agentica Web, Desktop, and IM are captured and recalled like the CLI; pure SDK sessions are excluded. `/ui` filters by source tool, and the search box matches source, session id, and short memory id. Re-run `memhub install-host` on hosts already installed. See [Release-v0.2.4](https://github.com/shibing624/memhub/releases/tag/v0.2.4).
- [2026/11/03] **v0.2.3**: Cursor sessions are captured automatically and recalled into the prompt before each question. `memhub remember` writes a personal memory. The host skill (`memhub-recall`) and the developer skill (`memhub-sdk`) no longer share a name. `memhub ui` uses a light theme and no longer jumps to the top or breaks IME composition. `versions.db` no longer stores a second copy of vectors. Re-run `memhub install-host` on hosts already installed. See [Release-v0.2.3](https://github.com/shibing624/memhub/releases/tag/v0.2.3).
- [2026/11/02] **v0.2.2**: Rebranded the repository and PyPI package to **`memhub`** (`pip install memhub`, `import memhub`, CLI `memhub`, config `~/.memhub/config.yaml`), promoting all Hub commands to top-level `memhub` subcommands (`memhub install-service`, `memhub install-host`, `memhub ui`, `memhub connect`, etc.). Added the local memory browser `memhub ui` (browse by project, hybrid search, edit into new versions, delete and sync); added CodeBuddy, OpenCode and WorkBuddy host integrations (8 hosts total); bounded sync targets with float16 vectors and snapshot compaction; scoped prompt-time recall to the current project and relevant dialogue passages (35/36 on the offline coding eval). See [Release-v0.2.2](https://github.com/shibing624/memhub/releases/tag/v0.2.2).
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
| [Examples](https://github.com/shibing624/memhub/blob/main/examples/README.md) | 6 runnable end-to-end example scripts |
| [Architecture & Design](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) | optional: [Architecture](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) · [Memory layers](https://github.com/shibing624/memhub/blob/main/docs/memory_layers.md) · [Write decisions](https://github.com/shibing624/memhub/blob/main/docs/jev-memory-design.md) |

## Community & support

> If memhub helps, a ⭐ star helps more people find it.

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
