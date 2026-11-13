<p align="center">
  <a href="https://github.com/shibing624/memhub">
    <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/logo.png" height="130" alt="memhub Logo" />
  </a>
</p>

<h1 align="center">memhub</h1>

<p align="center">
  <b>Local-first Personal Memory Hub &amp; Python Memory SDK across 18 AI Tools</b><br />
  Give Claude Code, Codex, Cursor, Agentica, OpenClaw, and 13 more tools one shared long-term memory that <b>evolves versions, distills coding fixes, and syncs via Git</b>.<br />
  <sub>100% Open-Source &amp; Local-First · Zero Account or Telemetry · #1 on LoCoMo, LongMemEval &amp; PersonaMem-v2</sub>
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
  <a href="#why-memhub">Why memhub</a> •
  <a href="#quickstart">Quickstart</a> •
  <a href="#benchmarks">Benchmarks</a> •
  <a href="#for-developers">Python SDK / MCP</a>
</p>

https://github.com/user-attachments/assets/1fba5e03-219d-414e-8c96-8e63a11200fd

---

## Why memhub

Project rules set in Cursor don't carry over to Claude Code or Codex. When your architecture or preferences change, append-only activity plugins keep feeding both outdated and current claims into the prompt. And hard-won bug fixes get buried inside raw terminal logs instead of becoming reusable knowledge.

`memhub` is not a tool-call activity logger — it maintains **one local, version-evolving memory** shared across your coding agents and applications:

| Dimension | `memhub` | `claude-mem` | `mem0` |
|---|---|---|---|
| **Memory model** | **Versioned memory**: new facts supersede stale claims with history preserved | Append-only tool observations; old and new claims accumulate | Generic chat CRUD without version chains or validity windows |
| **Coding awareness** | **Distills reusable bug fixes + live project rules** with file/branch drift checks | Logs file reads, edits, and session summaries | Plain-text snippets unaware of coding traces or branches |
| **Cold start & sync** | **`memhub scan` imports past chats across 18 tools**; serverless Git / cloud-folder sync | Captures sessions after install; cloud sync tied to official account | Custom ingestion scripts; sync requires hosted cloud or server |
| **Privacy & evals** | **100% local-first, no account or telemetry**; #1 on 3 public benchmarks | Installer prompts cloud trial & opt-out telemetry; no public memory eval | LoCoMo 65.6 (temporal 22 vs **78**) |

### Highlights

- **Shared across 18 AI tools, useful on Day 1**: Connects Claude Code, Codex, Cursor, Agentica, OpenClaw, and more to one local store. Incrementally imports existing local transcripts so past work isn't lost.
- **Automatic state evolution**: When preferences or technical decisions change, a dedicated write-adjudication model retires outdated statements and surfaces only current truth — while distilling long-term habits in the background (see [Architecture & Dual-System Design](https://github.com/shibing624/memhub/blob/main/docs/architecture.md)).
- **Purpose-built for coding**: Distills debugging sessions into *symptom → root cause & fix → touched files* while stripping noisy command output and automated background callbacks. Project-wide rules stay pinned, and renamed/deleted files trigger drift warnings.
- **Sub-200ms local recall & full ownership**: Hybrid semantic + lexical search runs locally (`~0.17s`, works offline) with budgeted progressive disclosure to save tokens. Includes `memhub ui` browser management, `<private>...</private>` redaction tags, and secret scrubbing.

---

## Quickstart

### 1. Connect your local AI tools

```bash
pip install "memhub[all]"

# 1. Start the background local Core (auto-starts at login, binds 127.0.0.1 only)
memhub install-service

# 2. Auto-detect and connect AI tools installed on this machine
memhub install-host
# Or target one tool: memhub install-host claude-code / codex / cursor / agentica / ...

# 3. (Optional) Import existing local session history
memhub scan --source all

# 4. Search from CLI or manage memories visually in your browser
memhub recall "what technical decisions did we make"
memhub ui                          # Opens http://127.0.0.1:8765/ui
```

<p align="center">
  <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/ui-mem.png" width="840" alt="memhub ui browser dashboard" />
</p>

Once connected, `memhub` runs invisibly: completed turns are distilled asynchronously in the background, and relevant project conventions, user preferences, and verified fixes are injected before each prompt (agents can also query/update via MCP).

<details>
<summary>Supported 18 AI tools &amp; integration matrix</summary>

| Host Tools | Prompt Recall | Auto Capture | MCP Tools | History Import (`scan`) |
|---|:---:|:---:|:---:|:---:|
| **Claude Code** / **Codex** / **Cursor** / **Agentica** | ✅ | ✅ | ✅ | ✅ |
| **OpenCode** / **CodeBuddy** / **WorkBuddy** / **Kimi Code** | ✅ | ✅ | ✅ | ✅ |
| **ZCode** / **Qoder** / **GitHub Copilot** / **AWS Kiro** | ✅ | ✅ | ✅ | ✅ |
| **Trae** | ✅ | ✅ | ✅ | — |
| **Kimi CLI** | MCP + Skill | ✅ | ✅ | ✅ |
| **DeepSeek Harness** / **Hermes** / **Cline** / **Pi** | MCP + Skill | `scan` | ✅ | ✅ |
| **OpenClaw** | Skill + CLI | `scan` | — | ✅ |

</details>

### 2. Sync across machines (zero server, zero re-embedding)

Every machine keeps a full local index for offline recall; devices exchange compressed change logs with zero LLM or embedding calls on the receiving machine:

```bash
memhub connect git git@github.com:you/my-memory.git                          # Recommended: private Git repo
memhub connect dir "~/Library/Mobile Documents/com~apple~CloudDocs/memhub"   # Or: iCloud / Dropbox / Syncthing folder
memhub connect ssh user@your-server                                          # Or: your own SSH server
```

See [Cross-Tool Memory Hub](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) and [Multi-Device Sync](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md).

---

## Benchmarks

### 1. Public Memory Benchmarks (Same LLM & Embeddings)

Evaluated from empty stores using **Qwen3.8-27B** and **bge-m3** across all systems — the memory engine is the only variable ([protocol & reproduction](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md)):

| System | LoCoMo (308 q) | LoCoMo Temporal | LongMemEval (24 q) | PersonaMem-v2 (476 q) | Median Ingestion Time |
|---|:---:|:---:|:---:|:---:|:---:|
| **memhub** | **74.7** | **78** | **91.7** | **50.0** | **503s** |
| hy-memory 2.0.4 | 66.6 | 61 | 70.8 | 43.5 | 2485s |
| mem0 | 65.6 | 22 | 83.3 | 47.9 | 808s |

`memhub` ranks **#1 across all three benchmarks**, leading temporal & state-update questions by **17–56 points** while ingesting ~**5× faster** than hy-memory.

### 2. End-to-End CLI Host Evaluation

Tested in an empty workspace on project codenames, timeouts, and pre-merge checks stored only in memory ([details](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md)):

| Mode | Claude Code | Codex | OpenCode | kimi-cli | kimi-code |
|---|:---:|:---:|:---:|:---:|:---:|
| Native (without memhub) | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 |
| **With memhub** | **5/5** | **5/5** | **5/5** | **5/5** | **5/5** |

---

## For Developers

Embed `memhub` directly in your own agents or backends via the standalone Python SDK, MCP server, or multi-tenant FastAPI REST service:

```bash
pip install memhub
```

```python
from memhub import SyncMemoryClient

with SyncMemoryClient() as memory:
    memory.add(content="I live in Shanghai and recently stopped eating spicy food.", user_id="alice")
    res = memory.search(query="recommend a restaurant for alice", user_id="alice")
```

The first run generates `~/.memhub/config.yaml` — plug in any OpenAI-compatible endpoints:

```yaml
jev_api_key: ""    # Write-decision model key (default: https://api.typesafe.ai/v1/systemone)
llm_api_key: ""    # Extraction & synthesis LLM key (supports custom llm_base_url)
embed_api_key: ""  # Embedding model key (supports custom embed_base_url)
```

- [Installation & Config](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md) · [Python SDK Quickstart](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md) · [Framework Integrations](https://github.com/shibing624/memhub/blob/main/docs/integrations.md) · [MCP & REST Server](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md) · [Runnable `examples/`](https://github.com/shibing624/memhub/tree/main/examples)

---

## Roadmap

- [x] Dual-system layered memory + write-adjudicated version evolution
- [x] Cross-tool Memory Hub across 18 AI tools, history import & `memhub ui` dashboard
- [x] Serverless multi-device sync (private Git / cloud folder / SSH)
- [x] Coding procedure distillation, pinned project rules & file drift checks
- [x] #1 on LoCoMo / LongMemEval / PersonaMem-v2 & 5/5 real CLI host eval
- [ ] Hosted HTTP MCP for stateless remote agents
- [ ] Multi-hop retrieval enhancements (LoCoMo multi-hop)
- [ ] Code-memory benchmark evaluations (CodeMemoryBench, etc.)

Feedback and feature requests welcome in [Issues](https://github.com/shibing624/memhub/issues).

## News

- [2026/11/12] **v0.2.4**: Expanded to 18 AI hosts; `memhub install-host` auto-detects installed tools; added `memhub doctor` and budgeted progressive disclosure; unified Agentica CLI/Web/Desktop/IM capture. See [Release-v0.2.4](https://github.com/shibing624/memhub/releases/tag/v0.2.4).
- [2026/11/03] **v0.2.3**: Automatic Cursor capture & prompt recall; added `memhub remember`; refreshed light-theme `memhub ui` and compact vector storage. See [Release-v0.2.3](https://github.com/shibing624/memhub/releases/tag/v0.2.3).
- [2026/11/02] **v0.2.2**: Unified package & CLI under `memhub`; launched `memhub ui` dashboard and float16 snapshot sync. See [Release-v0.2.2](https://github.com/shibing624/memhub/releases/tag/v0.2.2).

<details>
<summary>Earlier releases</summary>

- [2026/11/01] **v0.2.1**: Local-first multi-device change-log sync, removing persistent SSH tunnels. See [Release-v0.2.1](https://github.com/shibing624/memhub/releases/tag/v0.2.1).
- [2026/10/24] **v0.2.0**: Unified chat and coding memory APIs with automatic procedure distillation from tool traces. See [Release-v0.2.0](https://github.com/shibing624/memhub/releases/tag/v0.2.0).
- [2026/10/23] **v0.1.4**: Local cross-tool Memory Hub & #1 across LoCoMo / LongMemEval / PersonaMem-v2. See [Release-v0.1.4](https://github.com/shibing624/memhub/releases/tag/v0.1.4).
- [2026/06/18] **v0.1.0**: Initial open-source release. Full history in [CHANGELOG](https://github.com/shibing624/memhub/blob/main/CHANGELOG.md).

</details>

## Documentation

Full documentation: **[https://shibing624.github.io/memhub](https://shibing624.github.io/memhub)**

| User & Developer Guides | Architecture & Design Deep Dives |
|---|---|
| [Cross-Tool Memory Hub](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) · [Multi-Device Sync](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md) | [System Architecture](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) |
| [Installation & Config](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md) · [SDK Quickstart](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md) | [Four-Layer Cognitive Memory](https://github.com/shibing624/memhub/blob/main/docs/memory_layers.md) |
| [Framework Integrations](https://github.com/shibing624/memhub/blob/main/docs/integrations.md) · [MCP & REST API](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md) | [JEV Write Adjudication & Version Chains](https://github.com/shibing624/memhub/blob/main/docs/jev-memory-design.md) |
| [Public Benchmarks](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md) · [CLI Host Evaluation](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md) | [Runnable Examples](https://github.com/shibing624/memhub/blob/main/examples/README.md) |

## Community & Support

> If `memhub` saves you from repeating yourself across AI tools, please give it a ⭐ Star!

- **GitHub Issues**: [Report bugs or request features](https://github.com/shibing624/memhub/issues)
- **WeChat Group**: Add `xuming624` with note `llm`

<img src="https://github.com/shibing624/agentica/blob/main/docs/assets/wechat.jpeg" width="200" alt="WeChat Group QR" />

## Citation

```bibtex
@misc{xu2026memhub,
  author    = {Xu, Ming},
  title     = {memhub: Personal Memory Hub and Dual-System Layered Memory SDK for AI Agents},
  year      = {2026},
  publisher = {GitHub},
  url       = {https://github.com/shibing624/memhub}
}
```

## License & Acknowledgements

[Apache License 2.0](https://github.com/shibing624/memhub/blob/main/LICENSE) · Contributions welcome ([CONTRIBUTING.md](https://github.com/shibing624/memhub/blob/main/CONTRIBUTING.md)) · Built with thanks to [Chroma](https://github.com/chroma-core/chroma), [Kuzu](https://github.com/kuzudb/kuzu), [FastAPI](https://github.com/tiangolo/fastapi), [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk), and [mem0](https://github.com/mem0ai/mem0).
