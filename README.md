<p align="center">
  <a href="https://github.com/shibing624/memhub">
    <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/logo.png" height="130" alt="memhub Logo" />
  </a>
</p>

<h1 align="center">memhub</h1>

<p align="center"><b>多个 Agent 共用一份个人记忆；当场记下事实，事后看出规律。</b><br />Claude Code、Codex、Cursor、Agentica 等 18 种 AI 工具共享同一份本地记忆，支持跨设备同步与可视化管理。</p>

<p align="center">
  <a href="https://badge.fury.io/py/memhub"><img src="https://badge.fury.io/py/memhub.svg" alt="PyPI version" /></a>
  <a href="https://github.com/shibing624/memhub/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License Apache 2.0" /></a>
  <a href="https://github.com/shibing624/memhub/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-green.svg" alt="Python 3.11+" /></a>
  <a href="https://github.com/shibing624/memhub/issues"><img src="https://img.shields.io/github/issues/shibing624/memhub.svg" alt="GitHub issues" /></a>
  <a href="#社区与支持"><img src="https://img.shields.io/badge/wechat-group-green.svg?logo=wechat" alt="Wechat Group" /></a>
</p>

<p align="center">简体中文 | <a href="https://github.com/shibing624/memhub/blob/main/README_EN.md">English</a> | <a href="https://shibing624.github.io/memhub">在线文档</a></p>

https://github.com/user-attachments/assets/1fba5e03-219d-414e-8c96-8e63a11200fd

## 为什么选 memhub

在 Cursor 里交代过的代码规范，Codex 并不知道；你已经告诉 AI 三次「我现在不吃辣了」，它还在推荐川菜馆；聊了几十轮，它背得出每一句原话，却说不出你做事的稳定习惯。

### 1. 🔗 多个 Agent，一份记忆

`memhub` 把本机上的多个 AI 工具连到同一份个人记忆：**Claude Code、Codex、Cursor、Agentica、Hermes、CodeBuddy、OpenCode、WorkBuddy、Kimi（kimi-cli / kimi-code）、DeepSeek Harness、Trae、ZCode、Qoder、GitHub Copilot、AWS Kiro、Cline、Pi、OpenClaw** 读写同一个本地库，数据默认留在你的电脑上。

```bash
pip install "memhub[all]"

# 1. 启动后台常驻服务（开机自启；卸载用 memhub uninstall-service）
memhub install-service

# 2. 按需接入你正在用的 AI 工具（卸载用 memhub uninstall-host <工具名>）
#    不带参数时会自动探测本机已安装的工具；也可以直接指定：
memhub install-host
memhub install-host claude-code
memhub install-host codex
memhub install-host cursor
# 支持：claude-code | codex | cursor | agentica | hermes | codebuddy | opencode | workbuddy | kimi-cli | kimi-code | dsh | trae | zcode | qoder | copilot | kiro | cline | pi | openclaw
# 一键接入全部：memhub install-host all

# 3.（可选）把已有的历史会话导入为记忆
memhub scan --source claude-code   # 或 --source all 导入全部已安装工具的历史

# 4. 随时检索，或在浏览器可视化管理记忆
memhub recall "我之前做过哪些技术选择"
memhub ui                          # 打开 http://127.0.0.1:8765/ui 按项目查看、更新记忆
```

<p align="center">
  <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/ui-mem.png" width="820" alt="memhub ui 记忆管理页" />
</p>

接入后无需额外操作：每轮对话结束自动在后台沉淀记忆，下次开口提问时自动按当前项目召回相关记忆注入上下文，Agent 也能通过 MCP 主动查询。

**多台电脑共用一份记忆**：每台电脑都保留完整的本地记忆，检索只查本机（断网照常秒回），后台自动同步变更与向量，接收方不调任何模型。支持 3 种同步方式，任选其一：

```bash
# 方式一（最推荐）：用私有 Git 仓库保存与同步记忆（自带云端冷备，多机自动合并）
memhub connect git git@github.com:you/my-memory.git

# 方式二：用网盘同步文件夹（iCloud Drive / Dropbox / OneDrive / Syncthing）
memhub connect dir "~/Library/Mobile Documents/com~apple~CloudDocs/memhub"

# 方式三：连接自建服务器
memhub connect ssh user@your-server
```

详见 [跨工具记忆中枢（Hub）](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) 与 [多设备同步指南](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md)。

### 2. 🏆 同一个模型，记得更准、写得更快

抽取、作答、评分用同一个模型（Qwen3.8-27B），同一套 embedding（bge-m3），同一批题目——**只换记忆系统**：

| 记忆系统 | LoCoMo<br>(308 题) | LongMemEval<br>(24 题) | PersonaMem-v2<br>(476 题) | 单篇长对话写入耗时<br>(秒，越低越快) |
|---|:---:|:---:|:---:|:---:|
| **memhub** | **74.7** | **91.7** | **50.0** | **503** |
| hy-memory 2.0.4 | 66.6 | 70.8 | 43.5 | 2485 |
| mem0 | 65.6 | 83.3 | 47.9 | 808 |

**三场公开评测全部第一，写入耗时约为 hy-memory 的五分之一。** 评测协议、题型细分和复现命令见 [评测说明](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md)。

### 3. 🧠 快慢双系统：当场记事实，事后看规律

- **快系统（System 1）**：对话时实时记住你的事实、偏好和状态变化。你改了主意、换了城市或更新了技术方案，它自动用新版本替换旧说法并保留演化历史，回答永远用最新状态。
- **慢系统（System 2）**：在后台跨多次对话归纳你的长期习惯与行事风格，越用越懂你。

### 4. 🛠️ 工程排障经验也能留下来

修完一个棘手的 Bug、定下一条项目规范，不用手动记笔记。写代码的会话会自动把「问题 → 根因与方案 → 涉及文件」提炼成可复用的工程经验；下次遇到类似报错，直接把之前验证过的解法带出来。

## 安装与配置

```bash
pip install "memhub[all]"
```

配置 `~/.memhub/config.yaml`（首次运行自动生成），填入模型密钥即可：

```yaml
jev_api_key: ""    # 写入裁决模型密钥
jev_endpoint: https://api.typesafe.ai/v1/systemone

llm_api_key: ""    # 抽取与归纳模型密钥（OpenAI 兼容，地址可改）
llm_base_url: https://api.openai.com/v1

embed_api_key: ""  # 向量索引与检索密钥（OpenAI 兼容，地址可改）
embed_base_url: https://api.openai.com/v1
```

> 详细的安装说明、环境变量与进阶配置见 [安装与配置文档](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md)。

## 开发者接入（Python SDK / Agent 框架 / MCP / REST）

想在自己的应用或 Agent 里直接集成 `memhub`？请看开发者文档：

- **Python SDK 快速开始**（日常对话记忆、Coding 工程经验、同步/异步客户端）：[快速开始](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md)
- **Agent 框架集成**（Agentica、Claude Code Hook、Hermes、OpenClaw）：[生态集成](https://github.com/shibing624/memhub/blob/main/docs/integrations.md)
- **MCP 与 REST API 服务**：[MCP / REST 接入](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md)
- **可运行端到端示例**：[`examples/`](https://github.com/shibing624/memhub/tree/main/examples)

## 功能特性

- **多工具共享与多端同步** — 18 种 AI 工具共用本地记忆，支持通过私有 Git 仓库、网盘文件夹或自建服务器跨设备同步
- **浏览器可视化管理** — `memhub ui` 按项目和类型浏览、语义搜索、在线编辑（自动重算向量并同步）与删除记忆
- **版本演化** — 「住上海 → 搬北京」自动更新版本，旧版本留作历史，回答默认用最新的
- **分层存放** — 原始对话（L0）、事实与工程经验（L1）、个人画像（L2）、跨证据规律（L3）分层管理，按需召回
- **专用决策模型把关** — 新信息、重复还是更新，由轻量决策模型（JEV）精准裁决

## Roadmap

- [x] System 1 分层记忆 + 版本演化
- [x] System 2：跨证据归纳长期规律与画像目录（L3）
- [x] 专用决策模型把关写入
- [x] 跨工具记忆中枢：18 个 AI 工具（Claude Code / Codex / Cursor / Agentica / Hermes / CodeBuddy / OpenCode / WorkBuddy / Kimi / DeepSeek Harness / Trae / ZCode / Qoder / Copilot / Kiro / Cline / Pi / OpenClaw）共享记忆、历史会话导入、后台整理
- [x] 多设备同步：每台本地检索、离线可用，经私有 Git 仓库 / 同步文件夹 / 服务器交换变更
- [x] 工程排障经验自动沉淀与按项目召回
- [x] LoCoMo / LongMemEval / PersonaMem-v2 同场评测领先
- [x] 记忆可视化管理：`memhub ui` 按项目查看、搜索、编辑、删除记忆
- [x] 接入更多工具：DeepSeek Harness、Trae、ZCode、Qoder、Copilot、Kiro、Cline、Pi、OpenClaw 等，已达 18 种，[实测对照](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md)
- [ ] 云端 HTTP MCP：远程 Agent 直接连接托管记忆
- [ ] 多跳推理类问题的检索增强（LoCoMo multi-hop 仍有提升空间）
- [ ] 补充更多公开评测（CodeMemoryBench 等，待数据公开）

欢迎在 [Issues](https://github.com/shibing624/memhub/issues) 投票或提需求。

## News

- [2026/11/12] **v0.2.4**：宿主扩展到 18 种（新增 Kimi、DeepSeek Harness、Trae、ZCode、Qoder、GitHub Copilot、AWS Kiro、Cline、Pi、OpenClaw）。`memhub install-host` 不带参数时只接入本机已安装的工具。新增 `memhub doctor`、`memory_get` / `memhub memory-get`；召回按预算渐进披露工程经验，并附带与问法无关的实时项目约定。Agentica 的 Web、Desktop 与 IM 同 CLI 一样采集并注入记忆，纯 SDK 会话不入库。`/ui` 可按来源工具筛选，搜索框支持来源、会话 ID 与记忆短 ID。已安装的宿主需重跑 `memhub install-host`。详见 [Release-v0.2.4](https://github.com/shibing624/memhub/releases/tag/v0.2.4)。
- [2026/11/03] **v0.2.3**：Cursor 对话自动采集，并在提问前注入本项目记忆；新增 `memhub remember` 写入个人记忆。宿主 skill（`memhub-recall`）与开发者 skill（`memhub-sdk`）分开，避免互相覆盖。`memhub ui` 改为浅白风格，并修复点击条目跳回顶部、中文输入被打断的问题。本机 `versions.db` 不再重复存放向量。已安装的宿主需重跑 `memhub install-host`。详见 [Release-v0.2.3](https://github.com/shibing624/memhub/releases/tag/v0.2.3)。
- [2026/11/02] **v0.2.2**：项目与 PyPI 包名正式升级为 **`memhub`**（`pip install memhub`、`import memhub`、CLI `memhub`、配置 `~/.memhub/config.yaml`），所有 Hub 命令提升为 `memhub` 一级子命令（`memhub install-service`、`memhub install-host`、`memhub ui`、`memhub connect` 等）。新增本机可视化管理页 `memhub ui`（按项目浏览、混合检索、编辑生成新版本、删除同步）；新增支持 CodeBuddy、OpenCode、WorkBuddy（共 8 种宿主工具）；同步目标升级为 float16 向量压缩 + 快照折叠（不再无限增长）；每轮召回按项目限定并只截取相关对话段落，离线评测达到 35/36。详见 [Release-v0.2.2](https://github.com/shibing624/memhub/releases/tag/v0.2.2)。
- [2026/11/01] **v0.2.1**：多设备改为每台电脑本地检索，用私有 git 仓库、网盘文件夹或服务器交换变更日志，删掉了常驻 SSH 隧道。模型配置与 Hub 目录统一。工程记忆只保留问题和最终回答，工具输出不再送进模型。自动归纳改为一天最多一次。详见 [Release-v0.2.1](https://github.com/shibing624/memhub/releases/tag/v0.2.1)。
- [2026/10/24] **v0.2.0**：chat 与 coding 统一成同一套 `add()` / `search()`。带工具轨迹的工程会话在对话记忆之外沉淀 `kind="procedure"` 工程经验，共用存储、版本链和 JEV 裁决；`dual` 的 `digest()` 也覆盖工程经验。采集 Claude Code / Codex / Agentica 的工具轨迹，dual 模式空闲时自动 digest；`install-host` 改写 YAML 时保留注释。详见 [Release-v0.2.0](https://github.com/shibing624/memhub/releases/tag/v0.2.0)。

<details>
<summary>更早版本</summary>

- [2026/10/23] **v0.1.5**：修复 Coding 记忆接真实模型时写不进去的问题。抽取改为返回 `{"candidates": [...]}` 对象；去掉 coverage 硬门槛；同一批写入可以覆盖同一条旧方案；裁决只看当前版本边界内的记录；同一用户的并发写入按 scope 串行。详见 [Release-v0.1.5](https://github.com/shibing624/memhub/releases/tag/v0.1.5)。
- [2026/10/23] **v0.1.4**：新增本地单 Core 跨工具记忆中枢（支持 Claude Code / Codex / Cursor / Hermes / Agentica 会话增量导入、后台持久队列写入、开机自启）；升级 System 2 v3 聚类认知与版本桥召回；在 LoCoMo（74.7）、LongMemEval（91.7）、PersonaMem-v2（50.0）同场评测中全面领先 hy-memory 2.0.4 与 mem0。详见 [Release-v0.1.4](https://github.com/shibing624/memhub/releases/tag/v0.1.4)。
- [2026/10/15] **v0.1.3**：升级为四层记忆架构与 JEV 专用决策模型写路径，支持 target-first 关系裁决、版本演化链与历史回溯；新增 Agentica / Claude Code / Hermes / OpenClaw 原生生态集成与 Coding 记忆子系统。详见 [Release-v0.1.3](https://github.com/shibing624/memhub/releases/tag/v0.1.3)。
- [2026/06/20] **v0.1.2**：新增 `SyncMemoryClient` 同步客户端封装，支持首次运行自动生成默认配置，完善 REST / MCP / CLI 统一契约与在线文档站点。详见 [Release-v0.1.2](https://github.com/shibing624/memhub/releases/tag/v0.1.2)。
- [2026/06/19] **v0.1.1**：发布双系统分层记忆 SDK，支持语义 + BM25 + RRF 混合检索及 CLI / REST / MCP 接口。详见 [Release-v0.1.1](https://github.com/shibing624/memhub/releases/tag/v0.1.1)。
- [2026/06/18] **v0.1.0**：首版开源。

</details>

完整变更见 [CHANGELOG](https://github.com/shibing624/memhub/blob/main/CHANGELOG.md)。

## 文档

完整使用文档：**[https://shibing624.github.io/memhub](https://shibing624.github.io/memhub)**

| 文档 | 内容 |
|---|---|
| [跨工具记忆中枢（Hub）](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) | 18 个宿主工具接入、历史会话导入、`memhub ui` 可视化管理 |
| [多设备同步](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md) | 私有 `git` 仓库 / 网盘 `dir` / `ssh` / `http` 同步，每台本地检索 |
| [安装与配置](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md) | 安装方式、配置文件、环境变量与优先级 |
| [SDK 快速开始](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md) | Python 同步 / 异步客户端、Coding 记忆、多轮写入 |
| [生态集成](https://github.com/shibing624/memhub/blob/main/docs/integrations.md) | Agentica / Claude Code / Hermes / OpenClaw 接入 |
| [MCP / REST](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md) | MCP 工具与 HTTP REST 接口对照 |
| [评测说明](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md) | 协议、题集、复现与结果细分 |
| [示例说明](https://github.com/shibing624/memhub/blob/main/examples/README.md) | 6 个可运行的端到端示例脚本说明 |
| [架构与设计原理](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) | 可选阅读：[架构说明](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) · [四层记忆](https://github.com/shibing624/memhub/blob/main/docs/memory_layers.md) · [写入决策设计](https://github.com/shibing624/memhub/blob/main/docs/jev-memory-design.md) |

## 社区与支持

> 如果 memhub 帮到了你，欢迎点个 ⭐ Star，让更多人看到。

- **GitHub Issues** — [提交 issue](https://github.com/shibing624/memhub/issues)
- **微信群** — 添加微信号 `xuming624`，备注 "llm"，加入技术交流群

<img src="https://github.com/shibing624/agentica/blob/main/docs/assets/wechat.jpeg" width="200" alt="微信群二维码" />

## 引用

如果您在研究中使用了 memhub，请引用：

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

## 许可证

[Apache License 2.0](https://github.com/shibing624/memhub/blob/main/LICENSE)

## 贡献

欢迎贡献，见 [CONTRIBUTING.md](https://github.com/shibing624/memhub/blob/main/CONTRIBUTING.md)。

## 致谢

- [Chroma](https://github.com/chroma-core/chroma)
- [Kuzu](https://github.com/kuzudb/kuzu)
- [FastAPI](https://github.com/tiangolo/fastapi)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [mem0](https://github.com/mem0ai/mem0)
