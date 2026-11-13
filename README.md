<p align="center">
  <a href="https://github.com/shibing624/memhub">
    <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/logo.png" height="130" alt="memhub Logo" />
  </a>
</p>

<h1 align="center">memhub</h1>

<p align="center">
  <b>跨 18 种 AI 工具共享的本地个人记忆中枢 &amp; Python 记忆 SDK</b><br />
  让 Claude Code、Codex、Cursor、Agentica、OpenClaw 等共用同一份<b>会版本演化、懂代码经验、可 Git 同步</b>的长期记忆。<br />
  <sub>100% 开源本地运行 · 零账号零遥测 · LoCoMo / LongMemEval / PersonaMem-v2 同场评测第一</sub>
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
  <a href="#为什么选-memhub">为什么选 memhub</a> •
  <a href="#一分钟上手">一分钟上手</a> •
  <a href="#评测数据">评测数据</a> •
  <a href="#开发者接入">Python SDK / MCP</a>
</p>

https://github.com/user-attachments/assets/1fba5e03-219d-414e-8c96-8e63a11200fd

---

## 为什么选 memhub

在 Cursor 定好的工程规范，换到 Claude Code 又要重讲；技术方案改了三次，只做追加记录的插件把新旧说法一起塞进上下文；修过的棘手 Bug 淹没在几百条命令输出里，下次换个工具还得从头排查。

`memhub` 不做「工具调用流水账」，而是为你维护一份**自动更新状态、提炼排障方案、跨工具跨设备通用**的本地长期记忆：

| 对比维度 | `memhub` | `claude-mem` | `mem0` |
|---|---|---|---|
| **记忆机制** | **版本演化记忆**：新结论自动替代旧说法，保留历史链 | 追加式工具观察日志，新旧结论并存累积 | 通用对话片段增删改，缺版本链与时效边界 |
| **Coding 能力** | **沉淀排障方案 + 项目红线常驻**，自动检测文件/分支变动 | 记录文件读写与命令摘要 | 纯文本片段，无代码轨迹与分支感知 |
| **冷启动与跨端** | **`memhub scan` 一键导入 18 种工具历史**；私有 Git / 网盘免服同步 | 安装后新会话起记；云同步绑定官方账号 | 需手写脚本导入；多端依赖云端或自建服务 |
| **隐私与基准评测** | **100% 本地优先、无账号无遥测**；3 项公开基准同场第一 | 默认引导云端试用与遥测（可关）；无公开记忆评测 | LoCoMo 65.6（时间推理 22 vs **78**） |

### 核心优势

- **18 种工具共享，装上第一天就懂你**：打通 Claude Code、Codex、Cursor、Agentica、OpenClaw 等本地工具；支持增量扫描导入本机已有历史会话，无需从零积累。
- **状态自动演化，回答只用最新结论**：偏好改变或方案迭代时，专用写入决策模型自动归档过期说法、启用新版本，并在后台跨会话归纳长期习惯（底层原理见 [架构与快慢双系统](https://github.com/shibing624/memhub/blob/main/docs/architecture.md)）。
- **专为写代码打磨**：自动把排障过程提炼为「报错症状 → 根因与解法 → 关联文件」，过滤大段命令输出与后台任务噪音；项目红线随提问自动带入，关联文件改名或切分支时主动提醒核实。
- **毫秒级本机召回，省 Token 且数据归你**：本机混合检索（`~0.17s`，断网可用），按预算折叠细节按需展开；支持 `memhub ui` 可视化增删改查、`<private>...</private>` 隐私标签与密钥脱敏。

---

## 一分钟上手

### 1. 安装并接入本机 AI 工具

```bash
pip install "memhub[all]"

# 1. 启动本机后台服务（开机自启，仅监听 127.0.0.1；卸载用 memhub uninstall-service）
memhub install-service

# 2. 自动探测并接入本机已安装的 AI 工具（卸载用 memhub uninstall-host <工具名>）
memhub install-host
# 也可指定单个：memhub install-host claude-code / codex / cursor / agentica / ...

# 3.（可选）一键导入本机已有的历史会话
memhub scan --source all

# 4. 终端检索，或在浏览器可视化管理记忆
memhub recall "我之前做过哪些技术选型"
memhub ui                          # 打开 http://127.0.0.1:8765/ui
```

<p align="center">
  <img src="https://raw.githubusercontent.com/shibing624/memhub/main/docs/public/ui-mem.png" width="840" alt="memhub ui 记忆管理页" />
</p>

接入后全程无感运行：每轮对话结束后台异步整理入库；下次在任意工具提问前自动召回当前项目规范、个人偏好与相关排障经验注入上下文，Agent 也可通过 MCP 主动查询或更新。

<details>
<summary>查看已支持的 18 种 AI 工具与接入能力表</summary>

| 宿主工具 | 提问自动注入 | 会话自动采集 | MCP 工具 | 历史导入 (`scan`) |
|---|:---:|:---:|:---:|:---:|
| **Claude Code** / **Codex** / **Cursor** / **Agentica** | ✅ | ✅ | ✅ | ✅ |
| **OpenCode** / **CodeBuddy** / **WorkBuddy** / **Kimi Code** | ✅ | ✅ | ✅ | ✅ |
| **ZCode** / **Qoder** / **GitHub Copilot** / **AWS Kiro** | ✅ | ✅ | ✅ | ✅ |
| **Trae** | ✅ | ✅ | ✅ | — |
| **Kimi CLI** | MCP + Skill | ✅ | ✅ | ✅ |
| **DeepSeek Harness** / **Hermes** / **Cline** / **Pi** | MCP + Skill | `scan` | ✅ | ✅ |
| **OpenClaw** | Skill + CLI | `scan` | — | ✅ |

</details>

### 2. 多台电脑同步（零服务器、不重算向量）

每台电脑保存完整本地库，检索只查本机；设备间仅同步压缩后的增量变更，接收端零模型调用：

```bash
memhub connect git git@github.com:you/my-memory.git                          # 推荐：私有 Git 仓库自动合并与冷备
memhub connect dir "~/Library/Mobile Documents/com~apple~CloudDocs/memhub"   # 或：iCloud / Dropbox / Syncthing 目录
memhub connect ssh user@your-server                                          # 或：自建 SSH 服务器
```

详见 [跨工具记忆中枢（Hub）](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) 与 [多设备同步指南](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md)。

---

## 评测数据

### 1. 同模型公开基准测试

抽取、作答、判分统一使用 **Qwen3.8-27B** 与 **bge-m3**，从空库构建，唯一变量为记忆系统（[评测协议与复现](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md)）：

| 记忆系统 | LoCoMo (308 题) | LoCoMo 时间推理 | LongMemEval (24 题) | PersonaMem-v2 (476 题) | 单篇长对话写入耗时 |
|---|:---:|:---:|:---:|:---:|:---:|
| **memhub** | **74.7** | **78** | **91.7** | **50.0** | **503s** |
| hy-memory | 66.6 | 61 | 70.8 | 43.5 | 2485s |
| mem0 | 65.6 | 22 | 83.3 | 47.9 | 808s |

三项基准全部第一；时间与状态更新类问题领先 **17～56 分**，写入速度约为 hy-memory 的 **5 倍**。

### 2. 真实 CLI 宿主端到端实测

在空工作目录下考核仅存于记忆中的项目代号、超时配置与合并门禁命令（[实测明细](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md)）：

| 模式 | Claude Code | Codex | OpenCode | kimi-cli | kimi-code |
|---|:---:|:---:|:---:|:---:|:---:|
| 原生（无 memhub） | 0/5 | 0/5 | 0/5 | 0/5 | 0/5 |
| **接入 memhub 后** | **5/5** | **5/5** | **5/5** | **5/5** | **5/5** |

---

## 开发者接入

核心记忆引擎可作为独立 Python SDK、MCP 服务或多租户 REST API 嵌入自有 Agent 与应用：

```bash
pip install memhub
```

```python
from memhub import SyncMemoryClient

with SyncMemoryClient() as memory:
    memory.add(content="我住在上海，最近开始不吃辣了。", user_id="alice")
    res = memory.search(query="给 alice 推荐一家餐厅", user_id="alice")
```

首次运行会在 `~/.memhub/config.yaml` 生成配置模板，填入任意 OpenAI 兼容模型与网关即可：

```yaml
jev_api_key: ""    # 写入裁决模型密钥（默认 https://api.typesafe.ai/v1/systemone）
llm_api_key: ""    # 抽取与归纳模型密钥（支持 llm_base_url 自定义网关）
embed_api_key: ""  # 向量模型密钥（支持 embed_base_url 自定义网关）
```

- [安装与配置说明](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md) · [Python SDK 快速开始](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md) · [Agent 框架集成](https://github.com/shibing624/memhub/blob/main/docs/integrations.md) · [MCP / REST 服务](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md) · [端到端示例 `examples/`](https://github.com/shibing624/memhub/tree/main/examples)

---

## Roadmap

- [x] 快慢双系统分层记忆 + 专用决策模型版本演化
- [x] 跨工具记忆中枢：18 种 AI 工具共享、历史导入、`memhub ui` 可视化管理
- [x] 零服务器多端同步（私有 Git / 网盘文件夹 / SSH）
- [x] 工程排障经验沉淀、实时项目红线注入与文件漂移检测
- [x] LoCoMo / LongMemEval / PersonaMem-v2 公开评测第一 & 5 宿主实测 5/5
- [ ] 云端托管 HTTP MCP（供远程无状态 Agent 直接连接）
- [ ] 多跳推理检索增强（提升 LoCoMo multi-hop 表现）
- [ ] 代码记忆专项公开基准评测（CodeMemoryBench 等）

欢迎在 [Issues](https://github.com/shibing624/memhub/issues) 提需求或交流。

## News

- [2026/11/12] **v0.2.4**：支持宿主扩展至 18 种；`memhub install-host` 默认自动探测本机已装工具；新增 `memhub doctor` 与预算制渐进披露召回；Agentica 全端采集打通。详见 [Release-v0.2.4](https://github.com/shibing624/memhub/releases/tag/v0.2.4)。
- [2026/11/03] **v0.2.3**：Cursor 自动采集与提问前注入；新增 `memhub remember`；`memhub ui` 升级浅白主题并优化检索与存储体积。详见 [Release-v0.2.3](https://github.com/shibing624/memhub/releases/tag/v0.2.3)。
- [2026/11/02] **v0.2.2**：包名与 CLI 统一升级为 `memhub`；新增可视化管理页 `memhub ui` 与向量压缩同步快照。详见 [Release-v0.2.2](https://github.com/shibing624/memhub/releases/tag/v0.2.2)。

<details>
<summary>更早版本</summary>

- [2026/11/01] **v0.2.1**：多设备改为每台本地检索 + 变更日志同步，移除常驻 SSH 隧道。详见 [Release-v0.2.1](https://github.com/shibing624/memhub/releases/tag/v0.2.1)。
- [2026/10/24] **v0.2.0**：统一对话与 Coding 记忆接口，支持从工具轨迹自动沉淀工程排障经验。详见 [Release-v0.2.0](https://github.com/shibing624/memhub/releases/tag/v0.2.0)。
- [2026/10/23] **v0.1.4**：发布本地跨工具记忆中枢；LoCoMo / LongMemEval / PersonaMem-v2 同场评测第一。详见 [Release-v0.1.4](https://github.com/shibing624/memhub/releases/tag/v0.1.4)。
- [2026/06/18] **v0.1.0**：首版开源。完整历史见 [CHANGELOG](https://github.com/shibing624/memhub/blob/main/CHANGELOG.md)。

</details>

## 文档

完整文档站点：**[https://shibing624.github.io/memhub](https://shibing624.github.io/memhub)**

| 使用指南 | 进阶与架构设计 |
|---|---|
| [跨工具记忆中枢（Hub）](https://github.com/shibing624/memhub/blob/main/docs/memhub.md) · [多设备同步](https://github.com/shibing624/memhub/blob/main/docs/memhub-server.md) | [系统架构说明](https://github.com/shibing624/memhub/blob/main/docs/architecture.md) |
| [安装与配置](https://github.com/shibing624/memhub/blob/main/docs/getting-started/installation.md) · [SDK 快速开始](https://github.com/shibing624/memhub/blob/main/docs/getting-started/quickstart.md) | [四层认知记忆模型](https://github.com/shibing624/memhub/blob/main/docs/memory_layers.md) |
| [Agent 框架集成](https://github.com/shibing624/memhub/blob/main/docs/integrations.md) · [MCP / REST 接口](https://github.com/shibing624/memhub/blob/main/docs/mcp_integration.md) | [JEV 写入裁决与版本链设计](https://github.com/shibing624/memhub/blob/main/docs/jev-memory-design.md) |
| [公开基准评测](https://github.com/shibing624/memhub/blob/main/docs/benchmarks.md) · [CLI 宿主实测](https://github.com/shibing624/memhub/blob/main/docs/host-eval.md) | [端到端代码示例](https://github.com/shibing624/memhub/blob/main/examples/README.md) |

## 社区与支持

> 如果 `memhub` 对你有帮助，欢迎点一颗 ⭐ Star 支持项目发展！

- **GitHub Issues**：[提交问题或建议](https://github.com/shibing624/memhub/issues)
- **技术交流群**：添加微信 `xuming624`，备注 `llm`

<img src="https://github.com/shibing624/agentica/blob/main/docs/assets/wechat.jpeg" width="200" alt="微信群二维码" />

## 引用

```bibtex
@misc{xu2026memhub,
  author    = {Xu, Ming},
  title     = {memhub: Personal Memory Hub and Dual-System Layered Memory SDK for AI Agents},
  year      = {2026},
  publisher = {GitHub},
  url       = {https://github.com/shibing624/memhub}
}
```

## 许可证与致谢

[Apache License 2.0](https://github.com/shibing624/memhub/blob/main/LICENSE) · 欢迎贡献（[CONTRIBUTING.md](https://github.com/shibing624/memhub/blob/main/CONTRIBUTING.md)） · 感谢 [Chroma](https://github.com/chroma-core/chroma)、[Kuzu](https://github.com/kuzudb/kuzu)、[FastAPI](https://github.com/tiangolo/fastapi)、[MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)、[mem0](https://github.com/mem0ai/mem0)。
