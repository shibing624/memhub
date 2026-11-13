<p align="center">
  <a href="https://github.com/shibing624/memhub">
    <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/logo.png" height="130" alt="memhub Logo" />
  </a>
</p>

<h1 align="center">memhub</h1>

<p align="center">
  <b>本地优先、带版本演化的跨 Agent 个人记忆中枢与双系统记忆 SDK</b><br />
  让 Claude Code、Codex、Cursor、Agentica、OpenClaw 等 18 种 AI 工具共享同一份「越用越准、不冲突、可跨端同步」的长期记忆。<br />
  <sub>100% 开源本地运行 · Git 同步 · LoCoMo / LongMemEval / PersonaMem-v2 同场评测第一</sub>
</p>

<p align="center">
  <a href="https://badge.fury.io/py/memhub"><img src="https://badge.fury.io/py/memhub.svg" alt="PyPI version" /></a>
  <a href="https://github.com/shibing624/memhub/stargazers"><img src="https://img.shields.io/github/stars/shibing624/memhub?style=flat&color=yellow" alt="GitHub stars" /></a>
  <a href="https://github.com/shibing624/memhub/blob/main/LICENSE"><img src="https://img.shields.io/badge/license-Apache%202.0-blue.svg" alt="License Apache 2.0" /></a>
  <a href="https://github.com/shibing624/memhub/blob/main/pyproject.toml"><img src="https://img.shields.io/badge/Python-3.11%2B-green.svg" alt="Python 3.11+" /></a>
  <a href="https://github.com/shibing624/memhub/issues"><img src="https://img.shields.io/github/issues/shibing624/memhub.svg" alt="GitHub issues" /></a>
  <a href="#社区与支持"><img src="https://img.shields.io/badge/wechat-group-green.svg?logo=wechat" alt="Wechat Group" /></a>
</p>

<p align="center">
  简体中文 | <a href="https://github.com/shibing624/memhub/blob/main/README_EN.md">English</a> | <a href="https://shibing624.github.io/memhub">在线文档</a>
</p>

<p align="center">
  <a href="#一分钟上手">一分钟上手</a> •
  <a href="#为什么选-memhub">与 claude-mem / mem0 对比</a> •
  <a href="#评测与真实宿主实测">评测与实测</a> •
  <a href="#五大核心能力">核心能力</a> •
  <a href="#开发者接入">Python SDK / MCP</a>
</p>

https://github.com/user-attachments/assets/1fba5e03-219d-414e-8c96-8e63a11200fd

---

## 为什么选 memhub

> 不是又一个工具调用流水账插件，而是会演化、能解决矛盾的个人记忆中枢。

用 AI 写代码和日常对话时，真正的痛点从来不是「没地方存日志」，而是：

1. **工具割裂**：在 Cursor 定好的数据库规矩，换到 Claude Code / Codex / OpenClaw 又得重头交代；
2. **新旧打架**：你明明改过三次主意（从「喜欢吃辣」改成「戒辣」、从「用方案 A」改成「用方案 B」），只做追加摘要的工具会把**新旧说法一起塞进上下文**，Agent 照样按旧版本踩坑；
3. **重过程、丢经验**：把每次 `read_file` / `bash` 当成流水账堆起来，既费 Token，下次遇到同类报错也提炼不出「症状 → 根因 → 正确解法 → 适用分支」；
4. **历史沉没 & 云端绑定**：很多插件装完只能从下一轮开始记，过去几百个会话全浪费；或者引导登录账号、试用云端 Observer、带后台遥测。

**`memhub` 不是「Hook 操作流水账记录仪」，而是一个「会演化、能解决矛盾的本地认知记忆中枢 + Python 记忆引擎」：**

| 对比维度 | `memhub` | `claude-mem` | `mem0` |
|---|---|---|---|
| **核心定位** | **演化记忆中枢（Hub）+ 独立 Python SDK / REST / MCP** | Hook 驱动的工具活动记录插件（TS / Bun） | 通用对话记忆 SDK & 托管云服务 |
| **冲突与过期处理** | **专用决策模型（JEV）裁决** `OVERRIDE` / `NEGATE` / `TEMPORAL` / `CONFLICT`，新版本替换旧说法并保留完整演化链 | Append-only 操作观察日志与会话摘要，新旧观察持续累积 | LLM 抽取增删改合并，缺少显式双层冲突/有效期/历史链 |
| **写代码专属能力** | **独立 `procedure` 工程经验**（症状→根因→解法→文件+分支漂移检测）+ **实时项目约定目录（Project View）** | 记录工具读写观察与摘要，无独立工程经验结构与文件漂移校验 | 通用文本记忆，无 coding 工具轨迹与分支边界感知 |
| **Token 预算控制** | **3000 字预算渐进式披露** + `[procedure-index]` 目录折叠 + 同源 L0 增量裁剪 + **旧说法复活拦截** | 3 步 MCP 检索（`search` → `timeline` → `get_observations`） | Top-K 片段直接拼接 |
| **历史会话导入** | **`memhub scan` 一键增量导入 18 种工具已有历史对话**，装上第一天就懂你 | 主要从安装后新会话开始采集 | 需自写脚本调 API 导入 |
| **跨设备同步与隐私** | **100% 本地优先、无账号、无遥测**；`connect git/dir/ssh` 同步**带向量变更日志**，接收方 0 模型调用秒回 | 云同步绑定自家 cmem.ai，安装默认引导登录试用与匿名遥测（可关） | 多端共享需依赖托管云或自建服务端 |
| **同模同题公开评测** | **LoCoMo 74.7（时间推理 78）· LongMemEval 91.7 · PersonaMem-v2 50.0 · 写入 503s** | 未提供标准公开记忆评测 | LoCoMo 65.6（时间推理 22）· LongMemEval 83.3 · PersonaMem-v2 47.9 · 写入 808s |

---

## 一分钟上手

### 1. 安装并自动接入本机 AI 工具（18 种宿主）

```bash
pip install "memhub[all]"

# 1. 启动后台常驻 Core（开机自启，仅监听 127.0.0.1；卸载用 memhub uninstall-service）
memhub install-service

# 2. 自动探测并接入本机已安装的 AI 工具（配置保留原注释，卸载用 memhub uninstall-host <工具>）
memhub install-host
# 也可以手动指定：memhub install-host claude-code / codex / cursor / agentica / ...
# 一键全接：memhub install-host all

# 3.（可选）把本机过去攒下的历史会话一键导入为记忆
memhub scan --source all

# 4. 随时检索，或在浏览器可视化管理记忆
memhub recall "我之前做过哪些技术选型"
memhub ui                          # 打开 http://127.0.0.1:8765/ui
```

<p align="center">
  <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/ui-mem.png" width="840" alt="memhub ui 记忆管理页" />
</p>

接入后全程无感运行：每轮回答结束后台异步整理并写入版本链；下次在任意工具里提问前，几十毫秒内从本地召回当前项目约定、个人画像、相关事实与工程经验注入上下文（Agent 也可通过 MCP 主动调用 `memory_search` / `memory_get` / `conversation_search` / `memory_update`）。

<details>
<summary>查看已支持的 18 种 AI 工具（19 个入口）与接入能力矩阵</summary>

| 宿主工具 | 自动注入上下文 | 自动后台采集 | MCP 工具 | 历史导入 (`memhub scan`) |
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

### 2. 一条命令多台电脑同步（零服务器、不重算向量）

每台电脑都持有完整本地记忆与向量索引，**检索永远只查本机（断网可用）**；电脑之间只交换带 float16 压缩向量的变更日志与删除墓碑，**接收方不调用任何 LLM 或 Embedding 模型**：

```bash
# 方式一（最推荐）：用一个私有 Git 仓库保存与多机同步（自带云端冷备，每台电脑只写自己的子目录，免冲突）
memhub connect git git@github.com:you/my-memory.git

# 方式二：iCloud Drive / Dropbox / OneDrive / Syncthing 同步文件夹
memhub connect dir "~/Library/Mobile Documents/com~apple~CloudDocs/memhub"

# 方式三：连接自建服务器
memhub connect ssh user@your-server
```

详见 [跨工具记忆中枢（Hub）](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) 与 [多设备同步指南](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md)。

---

## 评测与真实宿主实测

### 1. 同模型、同题集公开基准测试

抽取、作答、评分全部采用同一底模（**Qwen3.8-27B**）与同一向量模型（**bge-m3**），从零建库——**唯一变量是记忆系统**：

| 记忆系统 | LoCoMo<br>(308 题) | LoCoMo 时间推理<br>(Temporal) | LongMemEval<br>(24 题) | PersonaMem-v2<br>(476 题) | 单篇长对话写入耗时<br>(秒，越低越快) |
|---|:---:|:---:|:---:|:---:|:---:|
| **memhub** | **74.7** | **78** | **91.7** | **50.0** | **503** |
| hy-memory | 66.6 | 61 | 70.8 | 43.5 | 2485 |
| mem0 | 65.6 | 22 | 83.3 | 47.9 | 808 |

- **三项公开评测全面第一**：LoCoMo 领先第二名 8+ 分，时间演化类问题 **78 vs 61 / 22**（拉开 17～56 分差距）。
- **写入速度快 ~5 倍**：单篇多轮长对话中位写入耗时仅为 hy-memory 的 **1/5**、比 mem0 快 **38%**。
- 评测协议、显著性检验与复现脚本见 [评测说明](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md)。

### 2. 真实 CLI 宿主端到端实测

隔离环境下写入只存在于记忆里的项目约定，实测 5 个支持无头运行的 AI 编程工具（[复现与明细](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md)）：

| 工具 | 原生（无 memhub） | 接入 memhub 后 |
|---|:---:|:---:|
| Claude Code | 0/5 | **5/5** |
| Codex | 0/5 | **5/5** |
| OpenCode | 0/5 | **5/5** |
| kimi-cli | 0/5 | **5/5** |
| kimi-code | 0/5 | **5/5** |

---

## 五大核心能力

### 1. 🧠 快慢双系统 + JEV 专用写入裁决，告别「记了又乱」
- **四层认知结构**：原始对话证据（`L0_RAW`）→ 事实 / 事件 / 意图 / 工程经验（`L1_FACT`）→ 长期画像与状态（`L2_PROFILE`）→ 跨证据抽象规律与人格目录（`L3_DERIVED`）。
- **JEV 关系裁决**：写入不是无脑 append，而是由专用轻量决策模型先比对历史版本，精准给出 `ADD_OVERRIDE`、`ADD_SUPPLEMENT`、`ADD_TEMPORAL`（带起止有效期）、`ADD_NEGATE`、`ADD_CONFLICT`、`ADD_HISTORICAL`（乱序旧日志不覆盖新结论）或 `SKIP` / `DELETE`。旧版本保留演化链 `supersedes` / `superseded_by`，默认只召回当前版本。
- **System 2 后台归纳（`dual` 模式）**：在后台对累积事实做层次聚类，跨多段对话提炼稳定的行为规律与长期偏好。

### 2. 🛠️ 为写代码深度打磨：工程经验 + 实时项目红线 + 漂移检测
- **自动沉淀 `procedure`**：结合 `tool_calls` 与 `exit_code` 执行信号（大块工具输出不入库），把排障过程提炼为「任务问题 → 根因与方案 → 涉及文件 → 分支/环境边界」，同时自动屏蔽 Agent 后台回调、定时任务等非人类轮次。
- **不看问法的 Project View 红线目录**：哪怕你只问「补个单测」，也会直接把该项目的「严禁碰生产库」「封版禁迁」等红线与目录带进上下文，避免关键词没撞上就漏规则。
- **本机实时文件漂移提示**：当某条历史工程经验引用的文件已被删除/改名，或当前 Git 分支不同时，在注入时主动提醒 Agent 先核实代码现状。

### 3. 📉 预算控制渐进式披露：省一半 Token，还能溯源原话
- 注入默认受 `3000` 字 / `120` 行预算约束：高相关工程经验展全文，其余折叠成单行 `[procedure-index id=...]` 目录项；同源 `L0_RAW` 会剔除已表达内容并拦截已被推翻的旧说法，只保留尚未覆盖的日期、联系人等增量细节。
- 需要完整步骤或原话确认时，Agent 通过 `memory_get(id)` 或 `conversation_search(source_id=...)` 二跳按需展开，避免每轮上下文无节制膨胀。

### 4. ⚡ 毫秒级本机混合召回 + 跨 Worktree 项目自动对齐
- 语义向量（Chroma）+ 共享倒排 BM25 索引（`LexicalIndex`）+ RRF 融合，本机 5,600+ 条记忆稳态召回约 **0.17s**，不拖慢每一次回车。
- 自动识别 Git 主仓库、`git worktree`、子模块与 `origin` 远端 `org/repo` slug：你在多个 worktree 目录或两台不同路径的电脑上开发同一项目，记忆自动归并到同一个项目下，不同名私有仓也不串台。

### 5. 🔒 彻底的隐私与自愈保障
- **`<private>...</private>`**：任意轮次中包裹隐私片段，标签外内容照常沉淀，标签内绝不落盘。
- **大图裁剪与可选 Secret 脱敏**：自动剥离内联 Base64 截图；开启 `redact_secrets: true` 后落盘前自动脱敏 20+ 种高置信度凭证。
- **浏览器管理页 `memhub ui` + `memhub doctor`**：可视化按项目/来源工具/会话 ID 搜索、在线编辑（自动重算向量并同步）或删除；内置写队列指数退避重试与健康告警。

---

## 开发者接入

想在自己的 Agent、RAG 应用或服务端（Python SDK / Agent 框架 / MCP / REST）直接调用 `memhub`？核心引擎完全独立开放：

```bash
pip install memhub
```

```python
from memhub import SyncMemoryClient

with SyncMemoryClient() as memory:
    memory.add(content="我住在上海，最近开始不吃辣了。", user_id="alice")
    res = memory.search(query="给 alice 推荐一家餐厅", user_id="alice")
```

- **Python SDK 快速开始**（同步/异步 `MemoryClient`、Coding 工程经验、时间旅行 `as_of`）：[快速开始](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md)
- **Agent 框架集成**（Agentica `MemHubWorkspace`、Claude Code Hook、Hermes、OpenClaw）：[生态集成](https://github.com/shibing624/memhub/blob/main/docs/integrations.md)
- **MCP 与多租户 REST 服务**（`memhub-mcp` / `memhub serve-api`）：[MCP / REST 接入](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md)
- **端到端可运行示例**：[`examples/`](https://github.com/shibing624/memhub/tree/main/examples)

## 安装与配置

```bash
pip install "memhub[all]"
```

配置 `~/.memhub/config.yaml`（首次运行自动生成），填入模型密钥即可（支持任意 OpenAI 兼容网关或本地部署模型）：

```yaml
jev_api_key: ""    # 写入裁决模型密钥
jev_endpoint: https://api.typesafe.ai/v1/systemone

llm_api_key: ""    # 抽取与归纳模型密钥（OpenAI 兼容，地址可改）
llm_base_url: https://api.openai.com/v1

embed_api_key: ""  # 向量索引与检索密钥（OpenAI 兼容，地址可改）
embed_base_url: https://api.openai.com/v1
```

> 详细的安装说明、环境变量与进阶配置见 [安装与配置文档](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md)。

## Roadmap

- [x] System 1 分层记忆 + 版本演化（`OVERRIDE` / `NEGATE` / `TEMPORAL` / `CONFLICT`）
- [x] System 2：跨证据归纳长期规律与画像目录（L3）+ Kuzu 证据图投影
- [x] 专用决策模型把关写入
- [x] 跨工具记忆中枢：18 个 AI 工具共享记忆、历史会话导入、后台整理
- [x] 多设备同步：每台本地检索、离线可用，经私有 Git 仓库 / 同步文件夹 / 服务器交换变更
- [x] 工程排障经验自动沉淀、预算制渐进披露与实时项目红线视图
- [x] LoCoMo / LongMemEval / PersonaMem-v2 同场评测领先，5 宿主实测 0/5 → 5/5
- [x] 记忆可视化管理：`memhub ui` 按项目、来源工具浏览、搜索、编辑、删除
- [ ] 云端 HTTP MCP：远程 Agent 直接连接托管记忆
- [ ] 多跳推理类问题的检索增强（LoCoMo multi-hop 仍有提升空间）
- [ ] 补充更多公开评测（CodeMemoryBench 等，待数据公开）

欢迎在 [Issues](https://github.com/shibing624/memhub/issues) 投票或提需求。

## News

- [2026/11/12] **v0.2.4**：宿主扩展到 18 种（新增 Kimi、DeepSeek Harness、Trae、ZCode、Qoder、GitHub Copilot、AWS Kiro、Cline、Pi、OpenClaw）。`memhub install-host` 不带参数时只接入本机已安装的工具。新增 `memhub doctor`、`memory_get` / `memhub memory-get`；召回按预算渐进披露工程经验，并附带与问法无关的实时项目约定。Agentica 的 Web、Desktop 与 IM 同 CLI 一样采集并注入记忆，纯 SDK 会话不入库。`/ui` 可按来源工具筛选，搜索框支持来源、会话 ID 与记忆短 ID。已安装的宿主需重跑 `memhub install-host`。详见 [Release-v0.2.4](https://github.com/shibing624/memhub/releases/tag/v0.2.4)。
- [2026/11/03] **v0.2.3**：Cursor 对话自动采集，并在提问前注入本项目记忆；新增 `memhub remember` 写入个人记忆。宿主 skill（`memhub-recall`）与开发者 skill（`memhub-sdk`）分开，避免互相覆盖。`memhub ui` 改为浅白风格，并修复点击条目跳回顶部、中文输入被打断的问题。本机 `versions.db` 不再重复存放向量。已安装的宿主需重跑 `memhub install-host`。详见 [Release-v0.2.3](https://github.com/shibing624/memhub/releases/tag/v0.2.3)。
- [2026/11/02] **v0.2.2**：项目与 PyPI 包名正式升级为 **`memhub`**（`pip install memhub`、`import memhub`、CLI `memhub`、配置 `~/.memhub/config.yaml`），所有 Hub 命令提升为 `memhub` 一级子命令（`memhub install-service`、`memhub install-host`、`memhub ui`、`memhub connect` 等）。新增本机可视化管理页 `memhub ui`；新增支持 CodeBuddy、OpenCode、WorkBuddy；同步目标升级为 float16 向量压缩 + 快照折叠；每轮召回按项目限定并只截取相关对话段落，离线评测达到 35/36。详见 [Release-v0.2.2](https://github.com/shibing624/memhub/releases/tag/v0.2.2)。
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
| [宿主实测](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md) | 5 个 CLI 宿主原生 vs +memhub 实测对照与链路验证 |
| [示例说明](https://github.com/shibing624/memhub/blob/main/examples/README.md) | 6 个可运行的端到端示例脚本说明 |
| [架构与设计原理](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) | 可选阅读：[架构说明](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) · [四层记忆](https://github.com/shibing624/memhub/blob/main/docs/memory_layers.md) · [写入决策设计](https://github.com/shibing624/memhub/blob/main/docs/jev-memory-design.md) |

## 社区与支持

> 如果 `memhub` 帮到了你，欢迎点个 ⭐ Star，让更多开发者看到！

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
