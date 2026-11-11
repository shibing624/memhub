# Changelog

## Unreleased

- **渐进式记忆披露闭环、实时项目约定视图与全入口执行证据留存**：
  - **抽取职责与噪音收敛**：`CodingMemoryExtractor` 提示词明确区分可复用的工程隐性规则/根因/决策（latent rules）与读当前代码即可获知的临时会话状态（ephemeral state）；`Extractor.extract_system1` 排除助手单次操作过程播报（如“助手修改/运行/确认了…”），同时在含工具调用的混合会话与无工具架构讨论会话中继续保留具体的助手事实（地址、命令、参数、技术依据）与用户事实，不按“是否含工具调用”一刀切丢弃 `owner="agent"`。
  - **按 ID 精确展开记忆（`memory_get`）与有预算的渐进披露**：Core 新增 `GET /v1/hub/memories/{memory_id}`（`HubClient.get_memory`），支持完整 ID 或唯一 `>=8` 位短前缀精确读取单条记忆及其完整 `procedure` 结构；Hub MCP 新增 `memory_get(memory_id)` 工具，CLI 新增 `memhub memory-get <id>`，并同步更新 `memhub-recall` skill（已安装宿主可重跑 `memhub install-host`）。`Settings` 新增实验参数 `hub_inject_max_chars`（默认 `3000`）、`hub_inject_max_lines`（默认 `120`）与 `hub_procedure_full_score`（默认 `0.40`）：未达完整展开阈值或超出预算的 `procedure` 自动降级为带 `id`、任务标题、适用边界（`scope`/`branch`/`env`/`files`）与 `L0` 来源的单行 `[procedure-index id=...]` 目录项，避免截断命令或否定约束。
  - **同源 `L0_RAW` 增量压缩与旧说法复活拦截**：`memhub.retrieval.hybrid_engine` 新增 `novel_source_excerpt`，同源 `L0_RAW` 对话不再因 `source_node_id` 相同被整段删除，而是优先过滤已被召回记忆覆盖的重复段落，在压缩预算内保留尚未表达的日期、封版条件、回滚联系人等上下文；同时 `/v1/hub/recall` 标记同源中已被覆盖更新（`not is_latest`）、否定撤回（`negation`）或归档删除（`archived`）的旧陈述，防止 `L0_RAW` 原文兜底把已废止的旧规矩重新注入上下文。
  - **与 Query 措辞无关的实时项目约定视图（Project View）**：`/v1/hub/recall`（及 `memhub recall --workspace`）直接从本机库当前生效版本（`is_latest=True`）实时构建该 `workspace` 的项目约定与工程经验目录，与动态召回共享 `3000` 字预算并自动去重；解决提问未命中规矩关键词（如只问“补单测”未提“生产库”）时的红线漏召回，且在 `memory_update` / `memory_delete` / 版本替换后立即生效，无离线缓存延迟。
  - **全采集入口执行证据（`tool_signals`）与本机实时文件漂移提示**：`extract_tool_signals` 与 Hub 采集器（Agentica、Claude Code、Qoder、Codex、CodeBuddy、WorkBuddy、Pi、OpenClaw、Copilot、Cline、OpenCode、ZCode）在 `L0_RAW.custom["tool_signals"]` 中记录带步骤序号 `seq`、`call_id`、工具名、命令/路径摘要 `target`、时间戳 `at`、`ok`、`exit_code` 及有界脱敏报错摘要 `error` 的结构化执行证据（工具原始大输出仍不入库）；`memhub.hub.browse.inspect_local_procedure_files` 在本机工作区存在时对项目级 `procedure` 提供实时、可恢复的文件缺失与当前分支核验提示，不改写向量库、不降低检索分、不跨设备同步，也不误标 `user`/`global` 级通用规则。
- **修复工程记忆超过候选上限时抛 `CodingDecisionError` 导致后台写入失败，并增强写入队列自愈**：
  - `CodingMemoryReconciler`：当同一项目/分支累积的 `procedure` 当前版本超过 `reconcile_candidate_limit`（默认 64）时，旧实现先调用 `_fit` 截断页面却仍对被截掉的条目发题（提示词要求不确定时选 `INCLUDE`），且筛出的 `INCLUDE` 数一旦超过 `candidate_limit` 就直接抛 `CodingDecisionError: coding selected material exceeds candidate budget` 让整次写入失败；`MULTIPLE` 展开命中 0 个或多个 head / `UNSURE` 时也会抛 `CodingDecisionError` 中止整轮写入；同时 `0:target` 题干按固定 4000 字预留，候选项多时可能超出 `jev_max_question_chars`。现在分页严格保证题集与 `state["current"]` 一致、筛出项超上限时按 `(confidence, 新旧顺序)` 排序保留前 `candidate_limit` 条、`MULTIPLE` 无法唯一指向单个 head 时与 `NEED_CONTEXT` 一致退化为无 target 独立版本（不抛错）、`target` 阶段按实际最大单题长度裁剪最旧 head、原文加草稿超预算时优先用 `fit_dialogue` 压缩原文上下文。
  - `HubWriteQueue`：默认 `max_attempts` 由 5 提升至 8（指数退避覆盖约 1 小时短暂断网/502 抖动）；Core 启动（`start()`）时自动重排非幂等冲突的历史 `failed` 任务；运行期间一旦有新的完整写入成功，自动给刚耗尽重试预算的 `failed` 任务补跑一次恢复探测（仍失败则停在 `max_attempts + 1` 不死循环），无需每次手动执行 `memhub retry`。
- **修复 Hub 采集与写入边界的若干静默故障**：
  - 一个半写或格式异常的会话文件（如 Cline 正在重写的 `*.messages.json`）不再中止整轮 `memhub scan`：该文件跳过并计入 `errors`，游标不动，下次扫描重读；其余文件照常入库。Core 不可达仍会中止。
  - Hub 写入的幂等指纹只覆盖证据本身（`content` / `messages` / `session_id`），且按客户端提交的原文计算：重投时 `memory_at`（文件 mtime）、workspace 归一化结果或 `redact_secrets` 开关变化，不再触发永久 409。`hub.db` 旧行在启动时一次性迁移。
  - L0 写入时同时记录 `custom["workspace_remote"]`（origin `org/repo`）；跨机器按项目召回时两边 remote 不同即视为不同项目，同名目录的私有仓库不再串入。未记录 remote 的旧数据仍按目录名兜底。
  - `conversation_search(source_id=...)` 的短 ID 展开只读取 L0 的 id，再按命中 id 取原文，不再把全部 L0 原文读进内存；写入与展开路径上的正则清洗和 `.git` 探测移出事件循环。
  - `memhub doctor` 遇到损坏的 `hub.json` 或宿主配置文件（如手改 `~/.cursor/mcp.json` 多了逗号）时报告为检查项，不再直接崩溃。
  - `PUT /v1/hub/memories/{id}`（`memory_update`）同样剥离 `<private>` 并按开关脱敏；block 形状的 content 与 dict 形状的工具参数也会被清洗；PEM 私钥正则在大量未闭合头部下保持线性。
  - Trae 等内联采集的宿主：写入 Core 成功后才删除暂存的提问，Core 不可达时这一轮不再丢失。
- **修复长对话写入 embedding 超上游 token 上限**：原先只按 `embed_max_tokens × embed_chars_per_token` 截字符，中文、emoji 和代码混排时字符/token 比会低于估算值，截后的输入仍可能超过上游限制并被拒绝。现在额外按 UTF-8 字节数封顶（`≤ embed_max_tokens` 字节），字节级 BPE 下每个 token 至少覆盖一字节，因此截断输入不会超 `embed_max_tokens`；完整原文仍保留在 L0_RAW 并用于抽取。
- **项目标识归一化（Git Worktree / 子模块 / 子目录 / 跨机器 Remote 对齐）**：
  - `memhub.hub.browse` 新增 `canonical_workspace`、`resolve_workspace_root`、`git_remote_slug` 与 `parse_origin_slug`：无需派生 `git` 子进程即可直接解析 `.git` 目录或文件（支持 linked `git worktree`、`git submodule`、bare worktree），将任意子目录或 worktree 路径自动归并到主仓库根目录。
  - 跨机器同步召回时，`workspace_sessions` 同时按主仓库路径、`.git/config` 的 `origin` 远端 `org/repo` slug，以及远端机器不存在于本机的同名 Git 仓库目录名做匹配，解决多 worktree 并发开发或两台电脑克隆路径不同导致按项目召回割裂的问题。
- **渐进式披露（Progressive Disclosure）与 L0 来源 ID 溯源闭环**：
  - Core 新增 `GET /v1/hub/conversations/{source_id}?context_turns=1`（`HubClient.get_conversation`），支持用 `<relevant-memories>` 中的 8 位 L0 短 ID（如 `37c85fe9`）或完整 ID 直接拉取对应的 `L0_RAW` 完整原始对话及同一会话前后 `context_turns` 轮上下文。
  - Hub MCP `memory_search` 返回结果新增 `source_id`（8 位 L0 短 ID），`conversation_search` 新增 `source_id` 与 `context_turns` 参数（也支持直接把短 ID 传给 `query`）；CLI `memhub recall` 新增 `--source-id` / `-i` 与 `--context-turns` 选项，并同步更新 `memhub-recall` skill 模板。
- **`memhub install-host` 默认自动探测本机已安装的 AI 工具（破坏性变更）**：
  - `memhub.hub.hosts` 新增 `detect_installed_hosts` 与 `is_host_detected`（按各工具配置目录或 `PATH` 可执行文件探测）。
  - `memhub install-host` 不带参数时默认从 `all` 改为 `auto`，只接入本机已安装的宿主工具，不再在未安装工具的机器上创建十几个空配置目录。想恢复旧行为请显式执行 `memhub install-host all`。
- **`<private>` 隐私标签剥离与多模态 Base64 图片载荷清洗**：
  - 新增 `memhub.hub.redact`：支持 `<private>...</private>` 隐私块剥离（只去掉标签内的内容，同一轮里标签外的提问与 assistant 的回答照常入库；未闭合的 `<private>` 丢弃其后全部内容）。`<private>` 是用户主动声明的排除范围，与「让 Agent 记住某个 key」不冲突。
  - 自动清洗对话与工具参数中的内联 `data:image/...;base64,...` 大图及 Anthropic / Claude Read / MCP / OpenAI 多模态图片块（替换为 `<elided ...>`），防止浏览器截图等大块 Base64 撑爆 SQLite 与同步日志。
  - **自动 Secret 脱敏默认关闭**（`redact_secrets: false`，对应 `MEMHUB_REDACT_SECRETS`）：这是个人助理场景，用户说「记住我的 key 是 X」就该记住。设为 `true` 后，内置 20+ 种高置信度凭证正则（URL 密码、AWS Access/Secret Key、GitHub PAT、OpenAI `sk-`、Anthropic `sk-ant-`、Slack/Discord Token & Webhook、JWT、PEM 私钥、Google/GitLab/HF/npm/PyPI/Groq/xAI/Perplexity Key 等）会在落盘与同步前统一替换为 `<redacted type='...'/>`，只识别有固定格式的凭证；`password=xxx`、`Authorization: Bearer xxx`、`export FOO_TOKEN=xxx` 这类无固定格式的值不会被替换，敏感内容请用 `<private>` 主动排除。
- **新增 `memhub doctor` 诊断命令与后台故障显式告警注入**：
  - 新增 `memhub doctor`（支持 `--json`）：只读检查 `~/.memhub/config.yaml` 三把密钥、本机 Core 存活状态、`hub.db` 写入队列健康度、跨设备同步状态以及本机已探测宿主的接入完整性，并给出修复命令。
  - 当后台存在重试耗尽的 `failed` 写入任务或跨设备同步报错时，`/v1/hub/recall` 与 `memhub hook` 会在 `<relevant-memories>` 末尾自动追加醒目告警，让 Agent 在首句回复提醒用户运行 `memhub doctor` 或 `memhub retry`，避免后台静默失败。
- **新增接入 ZCode、Qoder、GitHub Copilot、AWS Kiro、Cline，以及 Pi、OpenClaw 零 JS 轻接入（共 18 种工具 / 19 个入口），并补齐 `memhub.integrations` 的 `memory_update` 与 `include_history`**：
  - **ZCode（`zcode`）**：在 `~/.zcode/cli/config.json` 写入 `mcp.servers.memhub` 与 `hooks.events`（`UserPromptSubmit` / `Stop`），只读解析 `~/.zcode/cli/db/db.sqlite`（与 OpenCode 同构的 `session` / `message` / `part` 表）。
  - **Qoder（`qoder`）**：在 `~/.qoder/settings.json` 写入 `mcpServers.memhub` 与 `hooks`（`UserPromptSubmit` / `Stop`），增量解析 `~/.qoder/projects/**/*.jsonl`。
  - **GitHub Copilot（`copilot`）**：在 `~/.copilot/mcp-config.json` 写入 MCP、在 `~/.copilot/hooks/memhub.json` 写入 `UserPromptSubmit` / `Stop`（提示词召回返回顶层 `additionalContext`），增量解析 `~/.copilot/session-state/*/events.jsonl`（跳过 `ephemeral` 流式块与 `source=="system"` 注入块，剥离工具写入文件内容）。
  - **AWS Kiro（`kiro`）**：在 `~/.kiro/settings/mcp.json` 写入 MCP、在 `~/.kiro/hooks/memhub.json` 写入 v1 独立 hook（提示词召回输出纯文本），增量解析 `~/.kiro/sessions/**/sess_*/messages.jsonl` 与同目录 `session.json`。
  - **Cline（`cline`）**：零 JS 轻接入，在 `~/.cline/data/settings/cline_mcp_settings.json` 写入 MCP、在 `~/.cline/skills/` 写入 skill，按消息下标游标增量解析 `~/.cline/data/sessions/*/*.messages.json`。
  - **Pi（`pi`）与 OpenClaw（`openclaw`）**：采用零 JS 轻接入（不引入 Node 扩展）；Pi 写入 `~/.pi/agent/mcp.json` 与 `~/.pi/agent/skills/`，OpenClaw 写入 `~/.openclaw/skills/`，两者共用 Pi v3 会话解析器读取 `~/.pi/agent/sessions/*/*.jsonl` 与 `~/.openclaw/agents/*/sessions/*.jsonl`。
  - **`memhub.integrations`**：`MemoryBackend.list()` 补齐 `include_history` 参数透传（修复 `_SyncMemoryProvider` 调用 `memory_list(include_history=...)` 时的参数报错），并在 `_SyncMemoryProvider`（Hermes / OpenClaw MemoryProvider）中新增 `memory_update` 工具。
- **MemHub 新增支持 Kimi（kimi-cli / kimi-code）、DeepSeek Harness、Trae，共 11 种宿主**（`memhub install-host kimi-cli|kimi-code|dsh|trae|all`）。Kimi 在 `config.toml` 追加带标记的 `[[hooks]]` 块并写 `mcp.json`，其余内容原样保留；kimi-code 提问时注入（hook 标准输出会被直接加进上下文，所以回复纯文本），kimi-cli 1.49 不读提示词 hook 的输出，只采集。DeepSeek Harness 把 MCP 写进 `~/.dsh/profiles/*/cordis.patch.yml` 的 `mcp-manager` 条目，会话读取 zstd 压缩的 `session.v4.jsonl.zstd`（新增 `zstandard` 依赖）。Trae 用 `~/.trae/hooks.json`，hook 载荷自带提问和回答，不读会话文件。`memhub scan --source kimi-cli|kimi-code|dsh` 可导入历史会话。
- **修复：MCP `memory_add` / `remember` 写入的记忆在召回时让提示词 hook 整体失败**：这类写入不带 `memory_at`，召回的原始对话缺这个字段，`UserPromptSubmit` hook 抛 `KeyError` 并被吞掉，该轮不注入任何记忆。现在写入按到达时间记时，hook 也容忍无时间的记录。
- **README 顶部增加宣讲视频**：中英文 README 在标题下嵌入宣讲视频。
- **Hub MCP、Hub CLI 与 `memhub-recall` skill 改为 agent 友好**：宿主 MCP 补齐 `memory_list` / `memory_update` / `memory_delete`，覆盖「改掉 / 撤销 / 忘掉约定」；工具描述写明参数、返回结构，`memory_add` 明确 `pending` 是成功终态，`conversation_search` 说明 `session_id` 的来源前缀。Hub CLI 补齐 `memory-list` / `memory-update` / `memory-delete`，与 `recall` / `remember` / `status` 一起作为 MCP 挂掉时的等价备选。skill 明确「优先用宿主内置 MCP 工具」，给出 CLI 入口 `python3 -m memhub.cli <子命令>` 和可直接执行的完整命令，去掉了硬约束章节。已安装宿主需重跑 `memhub install-host`。

## [0.2.3] — 2026-11-03

- **`memhub ui` 改版为极简浅白风格，并修复点击条目自动跳回顶部的 bug**：
  - **修复滚动跳顶与中文输入法中断**：此前每次点击条目展开、编辑或输入筛选词时，都会用 `root.innerHTML = ...` 整棵重建 `<aside>` 和 `<main>` 滚动容器，导致 `scrollTop` 归零跳回顶部，且打字时反复销毁 `<input>` 会打断中文拼音合成（IME composition）。现在外层骨架与搜索栏常驻挂载，展开/折叠/编辑改为单张卡片原位替换（`updateCardInPlace`），列表重绘保留 `scrollTop`，并处理 `compositionstart` / `compositionend`。
  - **极简浅白视觉风格**：改为浅灰白背景（`#f7f8fa`）+ 纯白卡片（`#ffffff`）+ 柔和语义色标签；工程经验的 `files` 改为独立文件胶囊展示；语义搜索保留搜索词并限定当前选中的项目（`workspace`）。
- **两份 skill 都放进仓库，名字按用途区分**：
  - `memhub/skills/memhub-recall/SKILL.md`：宿主用的 skill 模板，原先是 `hosts.py` 里的一段字符串。`install-host` 读这份模板，填上宿主名、`recall` / `remember` 命令后，写到 `~/.<宿主>/skills/memhub-recall/`。
  - `memhub/skills/memhub-sdk/SKILL.md`：给开发者手动安装的 SDK 使用说明（CLI、REST、MCP 对照，以及 scope），原先在 `skills/memhub-recall/`，`name` 改为 `memhub-sdk`。两份内容和用途都不同，之前用同一个名字装进同一个宿主会互相覆盖。
  - 两份都打进了安装包（`package-data`）。
- **新增 `memhub remember "<内容>"`，重写宿主 skill**：`remember` 和 MCP 的 `memory_add` 一样，交给本机 Core 写入并绑定个人 scope，不用传 `--user-id`。宿主 skill 改成一张表，列出每件事对应的 MCP 工具和没有 MCP 时的命令；并写明不要用 `memhub add` / `memhub search`（SDK 命令，不经过 Core，会写进当前目录下的 `./.memhub_data`），也不要去读 `~/.memhub` 下的库。此前 WorkBuddy 在没加载 MCP 时，为了写一条记忆，先后调用了 `add --help`、`list-scopes`，还去读了配置、数据库和源码，最后写进了工作目录下的另一个库。已安装的宿主需要重跑 `memhub install-host`。
- **Cursor 提问时自动注入记忆**：`memhub install-host cursor` 同时注册 `beforeSubmitPrompt`，召回结果以 `additional_context` 返回，项目取 `workspace_roots`。官方 hooks 文档里该事件的输出只列了 `continue` / `user_message`；Cursor 3.27.22 客户端实际会读取 `additional_context`，作为 `<system_reminder>` 随提示词发给模型（超过 1 万字转存文件）。已安装的用户需重跑 `memhub install-host cursor`。
- **Cursor 接入补齐**：
  - **自动采集**：`memhub install-host cursor` 会在 `~/.cursor/hooks.json` 的 `stop` 事件里挂上 `memhub hook cursor`，其它 hook 保持不动。Cursor 也会执行 `~/.claude/settings.json` 里的 Claude Code hook；带 `cursor_version` 的 payload 交给非 cursor 的 hook 时会被忽略，避免用 Claude 的格式去解析 Cursor 的 transcript。
  - **工具轨迹**：`tool_use` 块记为工具步骤，Cursor 的编码会话现在也会沉淀 `procedure`。`contents` / `old_string` / `new_string` 和 `ApplyPatch` 的补丁正文属于产出，不保留，只留下路径之类的参数。
  - **项目与时间**：项目路径由 `~/.cursor/projects/<slug>` 反推（逐级匹配现存目录；一个 slug 对应两个目录时不猜）。目录已不存在时，改用工具参数里的路径推断。时间取每条提示的 `<timestamp>`，没有时沿用上一条，第一条之前用文件的 mtime。本机 223 个会话、3308 轮实测：时间 100% 有值，项目路径覆盖 99.3%（没有覆盖到的主要是 `empty-window`）。
  - Hook payload 里的 `transcript_path` 为空时，按 `conversation_id` 找 transcript。
- **本机 `versions.db` 不再存第二份向量**：已提交的版本计划在 journal 里只保留节点 id、正文和元数据，向量只存在 Chroma 里；导出日志时再从 Chroma 读取。这期间被删除的节点不再随日志传出。真实库副本实测，`versions.db` 从 27MB 降到 4.3MB，其中 99% 是计划里重复存放的十进制向量。同步格式不变，旧设备推来的条目照常导入。已有的旧计划不做自动迁移；它们在导出时同样改从 Chroma 取向量。
- **CLI 提升为一级命令、`memhub/memhub` 重命名为 `memhub/hub`、默认 `storage_dir` 改为 `./.memhub_data`**：
  - 所有 Hub 命令直接作为 `memhub` 一级子命令（`memhub install-service`、`memhub install-host`、`memhub ui`、`memhub status`、`memhub recall`、`memhub scan`、`memhub connect`、`memhub disconnect`、`memhub sync`、`memhub serve` 等），无需再敲 `memhub hub ...`；独立多租户 REST 服务命令改为 `memhub serve-api`。
  - 包内子目录由 `memhub/memhub/` 重命名为 `memhub/hub/`（`memhub.hub`）。
  - `Settings.storage_dir` 默认值与 `config.default.yaml` 改为 `./.memhub_data`。
  - `README.md`、`README_EN.md` 与 `docs/` 全套文档更新为 `memhub`（个人记忆中枢 + 双系统分层记忆 SDK）叙事，统一一级 CLI 命令、8 种宿主工具、REST `memhub serve-api` 与当前无 `MERGE` 的写路径说明。

## [0.2.2] — 2026-11-02

- **项目与包名全面更名为 `memhub`（`pip install memhub`、`import memhub`）**：破坏性变更。Python 包名由 `dual_mem` 改为 `memhub`，PyPI 包名与 CLI 主命令由 `dual-mem` / `dual-mem-mcp` / `dual-mem-hook` 改为 `memhub` / `memhub-mcp` / `memhub-hook`，配置与 Hub 根目录改为 `~/.memhub`（环境变量前缀改为 `MEMHUB_`）。升级后请将 `~/.dual-mem/config.yaml` 移至 `~/.memhub/config.yaml`，并重新执行 `memhub hub install-service` 与 `memhub hub install-host all`。
- **每轮召回按项目限定，原始对话带时间、从旧到新排列**。离线评测（4 个项目 27 段真实改写会话、36 道 coding 题，按注入上下文逐点评审）里，只看 dual-mem 注入的上下文从 32/36 提到 35/36；同一份数据上 agentica 原生召回是 33/36。
  - `hub hook` 把宿主传来的 `cwd` 交给 Core（`/v1/hub/recall` 新字段 `workspace`）。`workspace_id` 随 `L0_RAW` 证据（`custom["workspace_id"]`）与 `procedure` 一同复制，Core 从本地同步库找出该项目的会话，事实、工程经验和原始对话只从这些会话里召回。别的项目的事实只在比本项目任何一条都更相关时才进来（问题本身就是在问那个项目）。画像（用户是谁、希望怎么干活）不分项目。没有 `cwd` 或这个目录还没有写入时不限定。
  - 原始对话每段标上时间、按时间从旧到新排列，标题写明「同一件事以最新的为准」。之前按会话 id 标注、按相关度排序，先后两次相反的说法看起来是并列的。
  - 注入的原始对话每段只取和提示词相关的段落（最多约 1600 字），复用 SDK 的 `source_excerpt`。一整段会话原样塞进去时，本机真实库里一次注入有 38K 字。MCP 的「查原文」仍返回完整对话。
  - hub 召回的事实和画像门槛从 SDK 默认的 0.4 / 0.3 降到 0.2：coding 提问和答案很少用同样的词，真实命中的融合分常在 0.2～0.4。SDK 默认值不变。
- **工程经验不再有 MERGE**。合并会生成一个新版本并退役旧版本，实测合并后的方案丢掉了旧版本里用户定的约束（例如测试命令）。对已有 head 的补充现在是 `ADD_SUPPLEMENT`：两个版本都保持现行，不带 target。破坏性变更：`UpdateType` 里去掉了 `MERGE`。
- **MemHub 新增支持 CodeBuddy、OpenCode、WorkBuddy，共支持 8 种宿主工具**（`agentica` / `claude-code` / `codebuddy` / `codex` / `cursor` / `hermes` / `opencode` / `workbuddy`）。
  - `dual-mem hub install-host codebuddy|opencode|workbuddy|all` 自动写入 MCP、recall skill 与自动召回/会话沉淀 Hook（CodeBuddy / WorkBuddy 写入 `~/.codebuddy/{mcp.json,settings.json}` 与 `~/.workbuddy/{mcp.json,settings.json}`；OpenCode 写入 `~/.config/opencode/opencode.json` 的 `mcp.dual-mem` 与 `~/.config/opencode/plugins/dual-mem-hub.js` 插件）。
  - `dual-mem hub scan --source codebuddy|opencode|workbuddy|all` 与 `hub capture` 支持增量读取 `~/.codebuddy/projects/**/*.jsonl`、`~/.workbuddy/projects/**/*.jsonl` 及 `~/.local/share/opencode/opencode.db`，连同工具调用（含 `filePath` 参数）与项目路径一并采集沉淀。
- **`dual-mem hub status` 时间字段改为人类可读格式，`hub disconnect` 输出明确状态**：`runtime.started_at`、`digest.last_at` 和 `sync.last_at` 由原先的 Unix 秒时间戳改为本地时间字符串 `YYYY-MM-DD HH:MM:SS`（`dual_mem.memhub.config.format_time`）。`hub disconnect` 不再输出容易被误解为「断开失败」的 `"disconnected": false`，改为输出 `"sync_enabled": false`、`"removed_sync"`（刚移除的同步目标或 `null`）与 `"message"`（`"sync disconnected"` 或 `"already disconnected (no sync target was configured)"`）。
- **宿主 Hook 合并成一条命令 `dual-mem hub hook <source>`**：按 stdin 的 `hook_event_name` 分派，`UserPromptSubmit` 召回注入，其余事件（Claude Code / Codex 的 `Stop`，agentica 的 `run.completed` / `session.ended`）后台采集。agentica 里原来的两个消费者 `dual-mem-hub`（采集）和 `dual-mem-hub-recall`（召回）合并为一个 `dual-mem`：装了 hub 就该召回，拆成两个只会让人以为可以只装一半，名字也看不出谁管什么。重跑 `dual-mem hub install-host agentica` 会按命令签名（`-m dual_mem.cli`）删掉旧的两个、写入新的一个，其它消费者不动。`hub hook-search` / `hub capture` 已删除，没有别名。升级后必须重跑 `dual-mem hub install-host <宿主>`：旧配置里的命令不存在时 typer 退出码是 2，宿主会把它当作「拒绝这条提示词」。
- **记忆管理页 `dual-mem hub ui`**：本机 Core 在 `/ui` 提供一个单文件页面，命令会带着令牌在浏览器打开它（令牌放在 URL 的 `#` 片段里，不进服务端日志）。
  - 左栏按项目和类型（工程经验 / 事实 / 画像 / 待办意图 / 原始对话）筛选；输入框即时过滤正文，回车走 Core 的混合检索。
  - 卡片展开后能看到完整方案、涉及文件、版本历史（旧 → 新），以及这条记忆来自哪段对话。
  - 编辑会生成一个新的当前版本，旧版本保留在历史里；工程经验只改方案，任务描述和检索键不变。删除只是让当前版本退役，历史还在。两种改动都会触发一次同步，其他设备跟着变。
  - 项目归属按结构推导，不看文本：工程经验带写入时的 `workspace_id`，它来源的对话和从同一段对话抽出的其它记忆通过 `source_ids` 继承同一个项目；普通聊天没有项目。
  - Core 新增 `GET /v1/hub/memories`（全部版本，新的在前）、`PUT` / `DELETE /v1/hub/memories/{id}`，都要 bearer 鉴权；对非当前版本改动返回 409，不存在返回 404。
- **旧版设备推到同步目标的条目，报错会指明是哪台设备、怎么处理**。format 2 要求向量打包传输，旧版 dual-mem 推上来的条目里向量还是数字列表，之前导入时只报 `TypeError: argument should be a bytes-like object ...`。现在报出这条条目的 `origin/seq`，并说明处理方法：先升级那台设备，再删掉目标里它的 `log/<origin>/` 目录，让它重新推送。
- **同步目标不再无限增长：向量压缩 + 快照折叠**。破坏性变更：同步格式升到 2，format 1 的目标会被拒绝。
  - 向量在同步时用 float16 + base64 传输，文件体积降到约 1/3，接收方仍然不调模型。
  - 负责 digest 的设备在日志累积到 400 条后，把日志折叠成 `snapshot/state-<epoch>.json`（也可以用 `dual-mem hub sync --compact` 手动触发）。`git` 目标用新的根提交加 `--force-with-lease` 替换历史，新设备只需拉一份快照；已有设备下次同步时自动跟随，本机未覆盖的条目不丢。
  - 被删除的节点 id 再次写入时追加 `revive` 日志条目，否则各设备的 tombstone 会把它拦下，导致设备之间不一致。
- **重启 Core 就是再执行一次 `dual-mem hub install-service`**。没有单独的 restart 命令。macOS 会 `launchctl unload` 后再 `load`，Linux 会 `systemctl --user restart`。
- **`hub connect git` 的同步提交只包含记忆日志**。`close` 不再 `git add -A`，只暂存 `log/` 和 `manifest.json`。同一仓库里的脚本、README、词云不会被导入记忆库，也不会被检索到，同步时也不会把它们的改动提交上去。拉取时用 `--autostash`，这些文件在本地有改动也不会挡住记忆同步。

## [0.2.1] — 2026-11-01

- **文档改成按使用来写**。快速开始去掉 `aclose()` 说明；架构、记忆分层、写入决策，以及 MemHub 里 Core 内部流程，标成可选阅读。写入决策页删掉过期的本地计时记录。
- **coding 会话的记忆只从对话正文来，工具输出不再进模型**。破坏性变更：删除 `procedure_tool_result_max_chars`，新增 `procedure_source_max_chars`（默认 24000）。
  - 原因：真实 agentica 编码会话导入 36 个批次失败 26 个。一类是 L0_RAW 把整段工具轨迹（文件内容、命令输出）当 embedding 输入，中文、代码与日志混排时实际 token 数超过按 2.5 字符/token 估算的上限，被上游以 8192 token 拒绝；另一类是 procedure 裁决把整段轨迹作为 JEV `state`，超出 `jev_max_question_chars`。
  - 现在：L0_RAW、procedure 抽取和 JEV 裁决都只看用户消息与 assistant 回答；工具调用只用来结构化提取 `files`。超出预算时用户消息全文优先保留，assistant 回答截到 2000 字，仍超出才从最早的轮次丢弃，最后一轮一定保留。MemHub 采集端也不再读取工具输出。
  - 只有工具消息、没有对话正文的写入直接按空内容返回，不再单独存一份工具轨迹。
  - embedding 输入的字符上限改为按 1.2 字符/token（`embed_chars_per_token`）估算：真实会话里代码与日志密集段实测约 1.2，原先的 2.5 会让截断后的输入仍然超限。LLM 预算仍用 `chars_per_token`。
- **`hook-search` 只说 `UserPromptSubmit` 一种协议**：agentica 改为原生支持与 Claude Code / Codex 相同的 `UserPromptSubmit` hook，删除为 agentica 单独写的 `prompt.submit` 回复分支；`install-host agentica` 的召回 consumer 订阅 `UserPromptSubmit`。需要 agentica 包含 `UserPromptSubmit` 支持的版本；旧配置重新运行 `dual-mem hub install-host agentica` 即可。
- **MemHub 多设备同步改为「每台本地检索 + 变更日志复制」，删除 SSH 隧道接入**。破坏性变更：`hub attach` / `hub detach`、`hub connect user@server` 建的常驻 `ssh -N -L` 隧道服务（`com.dual-mem.hub-tunnel` / `dual-mem-hub-tunnel.service`）、`DUAL_MEM_HUB_BASE_URL` / `DUAL_MEM_HUB_API_KEY` 全部删除；`hub.json` 改为同步目标配置，旧文件要删掉后重新 `connect`。已装的隧道服务需手动卸载（macOS `launchctl unload ~/Library/LaunchAgents/com.dual-mem.hub-tunnel.plist` 后删除该文件；Linux `systemctl --user disable --now dual-mem-hub-tunnel.service`）。
  - 原因：原方案每台电脑只是瘦客户端，每次输入提示词的 `hook-search` 都要经隧道到服务器做向量检索；隧道断开（合盖、换网络，最多 90 秒才判定断线）时记忆完全不可用，没有服务器的用户则根本用不了。
  - 现在每台电脑跑自己的 Core、保留完整的本地库，召回只读本机。`store/versions.db` 新增复制日志：每次变更（版本计划、L0 Evidence、元数据补丁、删除）按 `(origin, seq)` 记录并带向量，接收方直接写入、不调任何模型。合并按字段进行且与顺序无关（`is_latest` 只能变假，`supersedes` / `superseded_by` 取并集，删除留墓碑）；离线时两台电脑改了同一条记忆，同步后结果一致，两个新版本都保留。
  - `hub connect dir <文件夹>`（iCloud Drive / Dropbox / Syncthing / NAS，每台设备只写自己的 `log/<origin>/`）、`hub connect git <仓库>`（同样布局，rebase 不会冲突）、`hub connect ssh user@server`（每次同步一条 `ssh ... dual-mem hub relay` 命令，不常驻连接）、`hub connect http https://... --api-key`。新增 `hub disconnect`、`hub sync`；Core 新增 `/v1/hub/log/{heads,export,import}`、`/v1/hub/sync`，`hub status` 的 `sync` 字段给出上次同步结果与错误。
  - Core 在本机写入后（防抖合并）、每 5 分钟、以及召回发现上次拉取超过 1 分钟时在后台同步；召回本身不等网络。
  - dual 模式只有一台设备归纳：`ssh` / `http` 由服务器负责，`dir` / `git` 由第一台同步的设备负责（记在 `manifest.json`）。System 2 给已有 schema 追加的证据边改为随版本计划提交，复制到其它设备后图投影一致。
  - 同步目标记录 Embedding 模型与维度，配置不一致的设备拒绝同步。
  - 未实现 S3 / R2、Cloudflare D1 后端：挂载成本地目录后可以直接用 `dir`。
- **配置目录并到 `~/.dual-mem`**。模型配置从 `~/.dual_mem/config.yaml` 改为 `~/.dual-mem/config.yaml`，与 MemHub 的游标、队列、记忆库同一目录。不再读取旧路径。Python 包名仍是 `dual_mem`。
- **MemHub 自动 digest 改为每天最多一次，并删除 `--digest-idle`**。dual Core 在写入队列排空后检查：距上次成功自动 `digest()` 已满 `--digest-every` 小时（默认 24，`0` = 关闭自动 digest）且仍有待归纳证据才调用模型。原先 `--digest-idle` 默认 300 秒，每次停够 5 分钟就归纳，开会、吃饭会一天跑很多次；这个等待不再决定频率，已从 `hub serve` 去掉。成功时间写在 Hub 根目录 `digest-at`；失败不记，下次队列排空后再试。没有这个文件时，下一次满足条件会归纳一次，然后才开始 24 小时间隔。`hub status` 的 `digest` 为 `auto` / `min_hours` / `last_at` / `last_error`。评测集写完后自己调用 `digest()`。

## [0.2.0] — 2026-10-24

**破坏性变更**：删除 `MemoryClient.search_coding` / `SyncMemoryClient.search_coding`、`Settings.coding_enabled` / `coding_db_path`、`CodingMemoryStore` 与 `{storage_dir}/coding_memory.db`。旧 coding 库不迁移；chat 记忆（向量库、versions.db、图）格式不变，可以直接沿用。

- **chat 与 coding 统一**：场景是每条数据的属性，不再是一套独立子系统。
  - 带工具轨迹（`role="tool"` 或 assistant `tool_calls`）且 JEV 判为工程会话的 `add()`，照常做 chat 抽取，同时并行做 procedure 抽取，产出 `kind="procedure"` 的 L1 记忆。原先 coding 会话会跳过 chat 路径，用户在写代码时说的偏好（如「我习惯用 uv」）进不了画像；现在两者都写。
  - procedure 与 chat 记忆共用向量库、scope、版本链、演化链接、`apply_version_plan`、幂等回执与 System 2 队列；`update` / `delete` / `get` / `list` 一致适用。删除了 coding 模块里重复实现的 SQLite 版本链、发布协议与 `coding_{app_id}` 前缀隔离。
  - `search(procedure_limit=..., procedure_min_score=0.3)` 另跑一路只召回 procedure，结果在 `SearchMemories.procedures`，有独立配额与阈值，两组分数不互相比较。`procedure_limit` 默认 0，chat 检索结果与此前一致。`MemoryItem.procedure` 给出 `task / solution / search_keys / boundary_envs / boundary_scope / files / workspace_id / branch`。
  - chat 裁决候选池与 chat 各检索路（profile / proactive / normal / 派生融合）排除 procedure；procedure 裁决只看同版本边界内的 procedure head。
  - `add()` 新增 `workspace_id` / `branch`（procedure 的版本边界）；`WriteResult` 新增 `scene`（`chat` / `coding`）与 `procedure_ids`。
  - procedure 裁决判 `INVALID` 只丢弃那一条候选，`NEED_CONTEXT` 写成独立版本，不再让整次写入报 `CodingDecisionError`。
  - 新配置：`procedure_enabled`（默认 True）、`procedure_tool_result_max_chars`（默认 4000，单条工具结果超长时保留头尾）。
- **dual 覆盖工程经验**：procedure 写入同样登记 System 2 待处理 scope，`digest()` 的归纳输入包含 procedure；人格目录（catalog）只引用对话事实。
- **接口面打通**：REST（`POST /v1/memories/` 的 `workspace_id` / `branch`，`POST /v1/memories/search` 的 `procedure_limit` / `procedure_min_score`）、MCP、CLI（`dual-mem add --workspace-id/--branch`、`dual-mem search --procedure-limit`）与 `SyncMemoryClient` 同步支持。
- **MemHub**：
  - Claude Code / Codex 会话采集与 Claude Code Hook 保留工具调用和结果，并把会话的 cwd / git 分支作为 `workspace_id` / `branch`；原先只保留 user / assistant 文本，coding 记忆在集成路径上写不进去。`recall` / `hook-search` 默认带最多 3 条 procedure。`/recall` 在 `limit=0` 但 `procedure_limit>0` 时仍返回 procedures。
  - dual 模式的 Core 在写入队列空闲 `--digest-idle` 秒（默认 300，0 = 关闭）后自动 `digest()`；原先没有任何地方会触发，L3 永远不生成。`hub status` 显示自动 digest 设置。
  - 新增 `dual-mem hub connect user@server`：通过 SSH 读服务器 `runtime-token` 与 Core 端口，在本机空闲端口建 `ssh -N -L` 隧道并装成登录服务（断线重连），确认身份后写 `hub.json`。安全模型不变（Core 仍只听 loopback）；`hub detach` 同时卸载隧道服务。
  - Agentica 会话采集同样保留工具调用（`tool_calls` 与 `tool` 记录）和 `cwd` / `git_branch`；原先只读文本，agentica CLI 的 coding 会话会被当成纯 chat 写入。
  - `install-host agentica` / `hermes` 改写 YAML 时保留注释、引号与缩进（ruamel.yaml 往返）；原先用 PyYAML 整体重写，会丢掉 `~/.agentica/config.yaml` 里的全部注释。
  - 召回上下文里 procedure 显示为 `[procedure]`，原先与普通事实一样显示为 `[fact]`。
- **CLI**：`dual-mem digest` 新增 `--mode`；用 `--mode dual` 写入而配置不是 dual 时，原先 `digest` 直接返回 `processed=0`。
- **依赖**：`scikit-learn` 加入基础依赖。System 2 的事实聚类一直依赖它，但只在 `[dev]` 环境里被间接装上；干净安装后 `digest()` 报 `ModuleNotFoundError: sklearn`。新增 `tests/test_packaging.py`：核心模块导入的第三方包必须出现在 `[project].dependencies` 里。
- **`python -m dual_mem.cli`**：新增 `dual_mem/cli/__main__.py`。Core 服务、Hook 与后台 scan 改用 `-m dual_mem.cli` 启动，消除 `'dual_mem.cli.main' found in sys.modules` 的 RuntimeWarning；已装的旧 `-m dual_mem.cli.main` Hook 仍能被识别，重新 `install-host` 时会被替换，不会重复。
- **验证**：chat 路径的 LLM prompt、JEV 问题、召回顺序与分数在 system1 / dual 下与 v0.1.5 逐项一致（mock 指纹对比）；全量 pytest 与 AST 检查通过；真实模型验证 procedure 写入、方案演化（OVERRIDE）、`procedures` 分组与阈值（相关查询 0.40–0.64，无关 0.09–0.18），以及 dual digest。另在一台 Linux 外网服务器（Python 3.12，systemd --user）上用 0.2.0 wheel 做了端到端：`hub connect` 建隧道、Claude Code 会话经隧道写入同时产出 chat 记忆与 procedure、本机召回 `procedures` 分组、Core 空闲 5 分钟后自动 digest 生成 L3、隧道进程被杀后自动重连、重复 connect 幂等、`detach` 清理干净。
- **MemHub 采集 agentica 工具轨迹**：agentica 会话日志里的 assistant `tool_calls`、`type="tool"` 记录与 `git_branch` 现在进入 Turn 的 steps / branch；此前只读文本，agentica 的工程会话一律按 chat 写入、产出 0 条 procedure。
- **`hub install-host` 保留 YAML 注释**：agentica / Hermes 的 YAML 配置改用 ruamel.yaml round-trip 读写；此前 PyYAML 整体重写会清空用户配置里的注释与引号。`ruamel.yaml` 加入基础依赖。卸载后文件与安装前逐字节一致（有测试）。
- **MCP 适配 mcp 2.x**：`[mcp]` extra 改为 `mcp>=2.2,<3`，`dual-mem-mcp` 与 MemHub MCP 改用 `mcp.server.mcpserver.MCPServer`（mcp 2.x 删除了 `mcp.server.fastmcp`，原先装了 mcp 2.x 的环境里两个 MCP 入口启动即 ImportError）。`build_mcp()` / `build_mcp_backend()` 不再接收 `host` / `port`：它们属于 `server.run(transport="streamable-http", host=..., port=...)`。MemHub MCP 的日志级别设为 WARNING，避免每次工具调用一条 INFO 刷满宿主日志。新增 stdio 端到端测试（真实子进程 + MCP client 握手）。
- **召回格式**：`format_memories` 中 procedure 显示为 `[procedure]`（原为 `[fact]`）。
- **extras 可以单独安装**：REST 与 MCP 共用的 `MemoryOperations` 从 `dual_mem/api/operations.py` 移到 `dual_mem/operations.py`（`from dual_mem.api import MemoryOperations` 仍可用）。此前只装 `[mcp]` 时，`dual-mem-mcp` 经 `dual_mem.api` 连带导入 fastapi 而报 `ModuleNotFoundError`。`tests/test_packaging.py` 新增检查：屏蔽其它 extra 的模块后，`[mcp]` / `[api]` 各自的入口仍能构建。
- **文档**：README / README_EN 补充 MemHub 的写读流程与 `hub connect` 用法；`docs/memhub.md` 新增「记忆是怎么同步的」一节；`docs/mcp_integration.md` 改为 mcp 2.x 用法（`uvx --from "dual-mem[mcp]"`、`build_mcp` 不再接收 host/port、procedure 参数）；`docs/memhub-server.md` 写明模型地址须从服务器可达，以及 dual 模式自动 digest；安装文档补全核心依赖与 extras 说明。

- **MemHub 服务器文档**：新增 `docs/memhub-server.md`，说明在自己的机器上跑唯一 Core，以及每台电脑用 SSH 隧道 `attach`。这不是两台库互相合并。`docs/memhub.md` 里的隧道示例改成解释端口转发和 `runtime-token`。
- **README / README_EN / 文档首页**：开篇改为 chat 双系统。System 1 负责对话中的事实、画像和版本演化；`dual`（System 2）在显式 `digest()` 时跨证据归纳 L3。MemHub 多 Agent 共享保留为生态能力。版式改成「为什么选 dual-mem」分点展开，评测、安装、快速开始、特性、News、文档、引用分开。News 各条链到对应 Release。示例只点 `02_dual.py` 和 `06_coding_memory.py`。安装说明写明 `jev_api_key`、`llm_api_key`、`embed_api_key` 三者都要，三个地址都可以改。
- **`config.default.yaml`**：文件头写明三把钥匙和三个可改地址。首次生成的 `~/.dual_mem/config.yaml` 用的就是这份模板。键和默认值没有变。
- **示例不再回退内网 JEV 中转**：`examples/_common.py` 只使用配置里的 `jev_api_key` 或 `DUAL_MEM_JEV_API_KEY` / `DUAL_MEM_JEV_ENDPOINT`。随包发出的 `config.default.yaml` 与示例说明里不再出现内网地址。

## [0.1.5] — 2026-10-23

- **Coding 记忆修复（真实模型下此前无法写入）**：
  - 抽取输出改为 `{"candidates": [...]}` 对象。原先要求顶层 JSON 数组，但默认 `llm_json_mode=True` 只允许对象，`chat_json` 会把数组丢成 `{}`，导致任何真实 LLM 下抽取都报 `coding extraction requires a JSON array`。prompt 同时写明各字段类型（模型曾把 `boundary_envs` 输出成数组）。
  - 删除 coverage 硬门槛，与 chat 路径一致：JEV 判 `MISSED` 不再作废整次写入；空抽取直接 RAW_ONLY，不调 JEV。
  - 同一批多个候选可以 OVERRIDE / MERGE 同一个 head（head 退役一次并链接全部后继），原先报 `coding version target is no longer current`；同一 head 既替换又归档改为写入前报 `CodingDecisionError`。
  - JEV 只会看到 draft 所在版本边界（workspace / branch / boundary_scope / strict session）内的 head，不再因为选中跨边界 head 让整次写入失败。
  - snapshot → 裁决 → 发布按 scope 串行（与 chat 路径的 `commit_locks` 一致），同用户并发写不再报 `CodingConcurrentWriteError`；LLM 抽取仍并行。
  - `WriteResult.commit_passed` 与 chat 路径口径一致（追加了版本或归档了 head）；DELETE 不再报 False，SKIP 不再报 True。
  - 结构化 `content`（如多段 parts）原样保存在 L0，不再被 `str()` 成 Python repr。
  - 清理：`CodingWriter` 删除未使用的 `llm` 参数；删除未使用的 `truncate_messages` / `extract_tool_summary`；coding 模块的 `agent_id` 默认值统一为 `""`。
- **`examples/06_coding_memory.py`**：统一标准 OpenAI tool_calls 格式（修复 `tool_call_id` 与调用 id 不匹配），新增第 3 轮方案演化，演示旧方案被覆盖后检索只返回当前版本。
- **Coding 记忆文档**：`docs/getting-started/quickstart.md` 新增「Coding 记忆」用法章节（消息格式、经验字段、版本演化、返回值、调用代价、存储位置）；`docs/jev-memory-design.md` 的 coding 设计说明按当前实现重写；README / README_EN / `examples/README.md` 同步，README 示例改为虚构的 `app/db.py` 并修正 SQLITE_BUSY 根因描述。

## [0.1.4] — 2026-10-23

### 2026-10-23：README、文档与示例升级（v0.1.4）

- **README / README_EN**：使用原创口号（"Memory That Evolves With You — Across Every AI Tool."），按用户视角重写开篇痛点与核心价值；补充 `bge-m3` embedding 说明并优化单篇长对话写入耗时表达；新增 Coding 记忆快速上手代码与 `examples/` 完整可运行脚本表格；在线文档外置展示，全部开发者文档与 `docs/` 章节取消 `<details>` 折叠隐藏；`News` 仅保留发布的 tag 版本（隐藏 `v0.1.0` / `v0.1.1`），并将 README 内所有相对链接改为 GitHub 绝对链接以兼容 PyPI 页面跳转。
- **Coding 记忆修复与示例**：修复 `MemoryClient.search_coding` / `SyncMemoryClient.search_coding` 默认 `agent_id`（原 `"default_agent"`）与 `add()` 默认 `agent_id=""` 不一致导致未显式传 `agent_id` 时检索不到的问题；新增端到端示例 `examples/06_coding_memory.py`。
- **新增 `docs/benchmarks.md`**：结果摘要、评测协议、配对显著性、题型细分和复现命令。
- **新增 `docs/integrations.md`**：按"你在用什么工具"选择接入方式，包含 Agentica、Claude Code Hook、Hermes / OpenClaw 的接入代码。
- **`docs/memhub.md`、`docs/index.md` 重写**：先讲收益和三步上手，并完整展示单 Core 架构、鉴权、写入队列、云端单实例等细节。
- **`docs/getting-started/quickstart.md`**：并入原 README 的"两档模式怎么选"、REST / MCP / CLI 速查，以及推荐配置（`reconcile_relation_threshold`）。
- **`mkdocs.yml` 导航**：顺序改为 Home → Getting Started → MemHub → Integrations → Benchmarks → Developer guide。`mkdocs build` 无告警。

- **MemHub（`dual-mem hub`）**：借鉴 Hy-Memory MemHub，改为本地单 Core 架构。`hub serve` 独占 `~/.dual-mem`，自动生成 `runtime-token` 并通过 `runtime.json` 供同机 CLI/MCP/Hook 零配置发现；持久 `hub_writes` Evidence 队列的唯一 worker 写 Chroma/Kuzu/SQLite。Agentica / Claude Code / Codex / Cursor / Hermes 共用固定个人 scope。云上仍是同一单实例配合 SSH/Tailscale 隧道，通过高级命令 `hub attach --base-url ... --api-key ...` 显式接入，不复制或同步记忆库。
- **删除 MemHub 多主快照方案**：移除 `--user-id`、`--fs-*`、`--sync-secret`、`--keep-snapshots`、`--install-hosts` 以及 `hub export/import/push/pull/sync`，同时删除 `MulanFs`、客户端 Fernet 加密和 `VectorStore.export_nodes/merge_nodes`；宿主安装改为独立 `hub install-host`，API Key 不写入宿主配置。
- `MemoryClient.add_raw` / `SyncMemoryClient.add_raw` 新增 `request_id`，raw-only Evidence 现在与普通 `add` 一样支持确定性幂等重放。raw-only 回执与 `add` 分开存，L0 节点相同：同一 `request_id` 先 `add_raw` 再 `add` 会复用已存 L0 继续抽取，不再报 `IdempotencyConflictError`。
- **MemHub 写入不再丢**：`hub_writes` 入库即回执，扫描 / MCP 不再等 LLM（原先 120s 超时、失败后游标照样前进导致该轮永久丢失）。worker 失败按指数退避重试（默认 5 次，30s 起），用尽才 `failed`；同一请求重新提交或 `hub retry` 会重新排队。队列按事件时间（`memory_at`）而非到达顺序处理，冷启动多 CLI 导入最旧的先进 reconciler。新增 `hub extract` 为 `--raw-only` 导入的 Evidence 补跑抽取。
- **MemHub 运维**：`hub install-service` / `uninstall-service` 安装登录自启（macOS LaunchAgent / Linux systemd --user）；MCP 代理每次调用工具时才连接 Core，宿主先于 Core 启动也能用；`hub serve` 只接受 loopback 地址，token 在拿到进程锁后才写入（第二个 `serve` 不会再覆盖运行中 Core 的 token）；`hook-search` 注入包 `<relevant-memories>`；`/recall` 跳过 `limit=0` 的那一路；`status` 不再截断在 10 万条；删除未使用的 `uploads` 目录。`VectorStore.get_many(limit=None)` 表示不限条数。

### 2026-10-22：精简——删除无收益的开关（breaking）

本轮在 PersonaMem-v2 c20（476 题）与 LongMemEval val24 上用独立评测（benchmark `results/rsi/`）试过以下 Jev/LLM 扩展，**全部没有显著正收益**，代码已删除，不保留开关：

| 试验（已删除） | 结果（同 Qwen3.8-27B / 同 QA） |
|---|---|
| 写入裁决改为“全部追加” / 27B 裁决 | PM 50.63 / 51.26 vs Jev 50.00（不显著）；27B 写入慢 2.1× |
| 写入 Jev `score` 重要度 + 读侧先验 | 先验 ×0.2 显著变差（−8.2pp），×0.05 −1.7pp |
| 抽取时归一绝对事件日期（`event_at`） | PM +2.9pp 不显著；LME 稳定 −2 题（knowledge-update） |
| 读侧 Jev score / 27B 列表式重排到 top-10 | PM +0.2 / −1.1；LME −2 题 |
| S2 按需（Jev `noul` 判 S1 证据是否充分） | 与不读 schema 持平（8W/8L），常开 −1.3 |

同时删除此前已验证无收益的读侧 cross-encoder（`rerank_endpoint/model/api_key/timeout/candidate_limit`、`providers/rerank.py`、`ComponentFactory.reranker`），以及无任何代码读取的死字段 `s2_dedup_threshold`、`hybrid_evidence_boost_max`、`hybrid_evidence_saturate`。旧配置里这些键会被 `extra="forbid"` 拒绝，需要从 YAML/env 中去掉。

**推荐设置 = 当前默认值**：Jev 只用于写入关系裁决（target/action choice），候选只与 cosine ≥ `reconcile_relation_threshold` 的当前记忆比较，低于阈值直接追加、不调用 Jev；`derived_context_limit=0`（schema 只写不读）、`catalog_context_enabled=True`、`version_bridge_limit=20`。

**Jev 调用量 vs 阈值（精简后 SDK 重新写入，PM c20 + LME val24）**：

| `reconcile_relation_threshold` | 需 Jev 的候选 | PM c20 | LME val24 |
|---|---:|---:|---:|
| 0.60（默认） | 94.4%（原 pm-jev）/ 94.7%（重跑） | 50.00 / 53.36 | 22 / 19 |
| 0.65 | 85.3% | 50.21 | 20 |
| 0.70 | 68.6% | 51.05 | 19 |

- 存储分析（pm-jev，20 persona）：Jev 真正选中目标的候选 cosine p10=0.72、p50=0.84；SKIP 目标 p5=0.78；判 NONE 的候选与最近旧记忆 p50=0.70。阈值 0.70 可省掉约 1/3 Jev 调用，只丢约 7% 的关系判断。
- **同一默认阈值 0.60 两次重写 PM 相差 3.4pp、LME 相差 3 题**（Bamboo 偶发 502/429 丢写约 1% + 抽取/QA 非确定性）。阈值 0.65/0.70 的差异都在这个范围内，**无法区分**，因此默认保持 0.60；对 Jev 调用成本敏感的部署可设 0.70（PM 无损、Jev 调用 −27%，LME 上未测出差异）。写入耗时主要在 LLM 抽取，阈值对 wall time 影响约 −10%（680→600 s/persona）。

验证：445 passed / 1 skipped（删除 7 项 cross-encoder 测试，`tests/test_rerank.py` 只剩 BM25/RRF，改名 `test_bm25_rrf.py`）。

### 2026-10-20（晚）：System 2 v3（hy-memory 2.0.4 对齐）、PersonaMem-v2 驱动的 System 1 抽取修正（breaking）

- **S2 v3 写入**（`system2/system2_agent.py` 重写）：
  - 基础 schema：只用 ACTIVE/latest 的 L1/L2（不再携带旧版本历史）；**average-linkage cosine 聚类**（`system2_cluster_distance=0.35`，超大簇按 `system2_refine_distance=0.25` 再切，簇内 ≥`system2_dedup_cosine=0.92` 去重，<`system2_min_cluster_size=4` 不调用模型，每簇 ≤`system2_max_prompt_facts=12`）。hy-memory 的 DBSCAN（eps 0.45/0.35）在一个用户的记忆上会链成单簇（PM-v2 实测 134 条→1 簇），故改为平均链接。簇**顺序**处理，一簇一次调用，prompt 按 hy 的 “When [情境], the user [模式] — reflecting [洞察]” 原子/领域内/不可变规则；操作 `create_schema` / `add_evidence`；≥2 个独立原文来源；与已有 schema cosine ≥`system2_schema_dedup_cosine=0.68` 且共享证据时改为 add_evidence。删除逐 draft Jev 校验与 retrieval_text 双文本。
  - **跨域 core schema**（`system2_cross_domain_*`）：≥5 条基础 schema 时，LLM 生成去领域的行为抽象（存于 schema `custom.behavior_text`），行为空间 cosine >0.70 且内容空间 <0.60 的对用并查集成组，每组生成一条 core schema（可返回 null），证据继承成员。
  - **persona catalog**（`system2_catalog_*`）：与问题无关的画像目录——LLM 只从当前事实中**按编号选择原文**（8 个固定领域，每域 ≤4，总 ≤32，不改写），作为 `kind=persona_catalog` 的 L3，事实库变化（fingerprint）才重建，新版本 supersede 旧版本。
  - sweeper / catalog 失败只丢弃该产物（记日志），不影响同一次 digest 的其它产物。
- **读侧**：`SearchMemories` 新增 `catalog` 分组（每次检索附带当前 catalog）；`schema` 组只含 schema/core_schema（`schema_selection` 按 kind 过滤），forward 阈值 0.4、support ≥1。`flatten(limit)` = 有界证据 + schema + catalog。formatter `format_schema_block(derived)`：catalog 渲染为 “User profile (current recorded facts by area)”，schema 为无标签纯文本行（不再附证据，不再加 System 2 标题）。
- **默认值**：`derived_context_limit=0`（schema 只写不读）、`catalog_context_enabled=True`。依据 PersonaMem-v2 同库读侧消融（c20，476 题）：无派生 48.32%、+schema 46.85%（5W/12L）、+catalog **50.00%**（19W/11L）、两者 49.16%。
- **System 1 抽取（`extract_system1` prompt）**，均为通用规则、无关键词/正则：
  - 用户带来做任务的文本（润色的邮件、待翻译段落、帖子）中用户自述的活动/习惯/健康/物品/关系是一手证据；他人写的文本归他人。
  - “请忘记/不要记住”是**撤回**：不抽取被撤回的细节，只写一条“用户撤回了…，不得据此个性化”；Jev 的 `ADD_NEGATE` 描述包含撤回。
  - 具体、带个人色彩的提问本身记为一条事实（“用户问了 X”），不推断兴趣；泛泛/琐碎提问仍跳过。
  - 删除 “Do not infer preferences or traits from behavior”，改为 “记录用户陈述或所做之事，不泛化为更宽的偏好/特质”。
- **PersonaMem-v2（text 32k，Qwen3.8-27B，统一 QA prompt）**：调参集 c20（476 题）S1 46.01%（旧抽取）→ 48.32%（任务文本）→ 49.37%（撤回）→ **51.47%**（提问记录）；dual 52.10%；mem0 50.00%；长上下文 51.89%。**Holdout h20（另 20 persona / 488 题，未参与调参）：dual 52.66%、S1 50.61%、mem0 49.80%、长上下文 52.05%**（dual vs S1 26W/16L）。论文 DCPM（不同模型、协议未公开，绝对值不可比）：lite 46.85/54.10、full 49.36/59.30、mem0 42.53/41.25、长上下文 50.82/58.46。
- **验证**：452 passed / 1 skipped；S2 测试按 v3 契约重写（聚类、最小簇、≥2 原文、助手排除、非法操作丢弃、生成失败不重试、补证据不改写、旧版本不参与、跨域 core、catalog 选择/复用/重建）。

### 2026-10-20（下午）：System 2 v2 重构——行为 schema、证据门槛与独立读路（breaking）

参照 hy-memory 2.0.4 chat 路径（`system2_agent` / `schema_selection`）与 DCPM（arXiv 2606.09483）的高层记忆设计，S2 整体重写，不兼容旧数据格式与旧接口：

- **只产出一种派生记忆：行为 schema。**“When <情境>, the user <行为> — reflecting <驱动>”，一条一个规律。删除 S2 的 preference / summary / intention 产出：明确偏好、状态、计划已由 System1 存储，再上升到 S2 只会与原事实竞争。
- **写入：先做不调 LLM 的结构化准备，再按簇生成。**当前 L1/L2 记忆按 embedding 做平均链接聚类（cosine 距离 `system2_cluster_distance=0.35`，簇上限 `system2_max_prompt_facts`）；只有覆盖 ≥`system2_min_sources`（默认 2）个不同原文来源（`source_node_id`，即独立对话片段）、且归属用户的簇才调用模型。每簇一次生成，返回结构化操作：`create`（content + retrieval_text + evidence_ids）、`add_evidence`（给已存在的近邻 schema 补证据，内容不改写），或空列表。
- **校验：结构 + 语义，均无正则/关键词。**引用 id 必须在簇内、跨 ≥2 个原文来源；每条 draft 一次 Jev 判断“引用证据是否共同支持、情境是否明确且不超出证据、是否只是复述或泛化标签”。同 retrieval_text embedding 近重复（`s2_dedup_threshold`）丢弃。失败不修复、不重试；簇未提交时其记忆不标记已处理，下次 digest 再看。
- **存储：**schema 的 `custom` 记录 `source_ids`（证据记忆）、`raw_source_ids`（独立原文来源）与 `retrieval_text`；embedding 用 retrieval_text（情境+行为），content 给读者。图投影保留 DERIVED_FROM 边。
- **读取：schema 是 `SearchMemories.schema` 独立分组，永不占 System1 证据名额。**在 System1 结果定稿之后选择：反向（命中记忆沿证据边到达的 schema，按解释的命中记忆数排序，需 ≥`schema_min_support`）与正向（retrieval_text 与 query cosine ≥`schema_forward_min_score`）按 RRF 融合，上限 `derived_context_limit`。每个 schema 带 `supporting_ids`（它解释的已命中记忆）。`flatten(limit)` = 有界证据 + 其后追加 schema；删除 `derived_limit`、`_DERIVED_LAYER` 合并逻辑与旧 forward/reverse L3 融合进 profile 路。
- **formatter：**新增 `split_schema_hits` / `format_schema_block`：schema 作为证据之后的单独块，每条附至多 2 条它所解释的已命中记忆原文。
- **默认关闭读取（`derived_context_limit=0`，`schema_min_support=3`）。**理由是实测（见下）：在完全相同的 S2 v2 存储上，打开 schema 与关闭 schema 的配对对比为 PM −3（min_support=3）/ −4（=1），LME 0 / −1。schema 由 digest 照常写入，调用方显式设置 `derived_context_limit>0` 才读取。
- **验证：**450 passed / 1 skipped；S2 测试全部按新契约重写（聚类、来源门槛、助手归属排除、结构/Jev 拒绝、生成失败不重试、补证据不改写、历史仅作上下文、跨租户历史拒绝、默认不读、min_support）。

### 2026-10-20：版本桥召回、S2 独立配额与 24h 验收结论

- **版本桥召回（`version_bridge_limit`，默认 20）**：检索时同时查询已被取代（`is_latest=False`）的旧版本；命中旧版本后沿写入时记录的 `superseded_by` 边找到当前版本，当前版本继承旧版本的语义分 `max(own, inherited)`，旧版本本身不进结果。解决“用户提问只像旧措辞、与当前状态措辞无共享词”时召回不到最新事实的问题。纯结构关系，不使用任何正则、关键词或词表，语言无关。`0` 关闭。
- **S2 派生记忆独立配额（`derived_context_limit`，默认 8）**：`SearchMemories.flatten(limit)` 先让 System1 证据（facts / profile / raw）占满 `limit`，L3_DERIVED 在其之上最多追加 `derived_limit` 条，不再与事实争抢名额。`derived_limit=-1`（未设置）与旧的合并排序完全一致；`0` 不返回派生记忆。
- **验证**：450 passed / 1 skipped（新增版本桥召回、flatten 配额测试）。
- **24h 验收结论**（Qwen3.8-27B FP8 统一做抽取 / S2 / QA；mem0 同模型；PM 84+28 题、LME 72+24 题；详见 benchmark `CHAGNELOG.md` 2026-10-20）：
  - System1 vs mem0：PM 验收 **+3.6pp**、验证 +7.1pp；LME 验收 **+30.6pp**、验证 +8.3pp。PM **未达 +10pp**，LME 达标。
  - dual vs mem0：PM 验收 −1.2pp（独立配额后 +2.4pp）；LME 验收 +27.8pp。**dual 在两个 benchmark 上都不优于 System1**：同一 dual 存储关闭/打开派生记忆对照，PM 70 vs 64，LME 64 vs 63。派生记忆多为跨事实的泛化偏好或总结，既占召回名额，又把 QA 引向与之措辞相近的错误选项；独立配额只挽回一半。
  - dual ≥ mem0+20pp 仅在 LME 验收集达成（+27.8pp，仍低于 System1 的 +30.6pp）；PM 未达成。当前 S2 生成策略需重新设计（见 benchmark 变更日志的下一步）。

### 2026-10-18：单次写入、来源证据与有界资源

- **Jev 材料局部化**：每个候选只比较自身相似的当前 head/较早候选；无相关目标或 `NONE` 直接追加。默认 `reconcile_batch_size=1`。不再发送原文、representation/coverage 问题，也不把 revision、source/session 标识等控制面字段送进语义判断；正文、日期、类型、归属与历史值保留。CAS 与 provenance 仍在存储层。
- **真正移除传输重试**：Jev/embedding 不再循环或退避；LLM/embedding 的 OpenAI 客户端 `max_retries=0`；旧 extractor 的空结果重抽也删除。移除 `jev_retry_attempts`、`embed_retry_attempts`、`embed_retry_base_delay`、`extract_retry_on_failure`。旧 YAML 有这些键时应删除，不能用静默忽略掩盖配置错误。
- **召回统一尺度**：词法召回可以补充语义候选池外的精确匹配，补入候选保留真实 cosine；所有候选共同校准 BM25。profile 副路不再以原始 cosine 覆盖混合分，L3 的内部 RRF 分也不再直接与事实分比较。
- **来源证据（`raw_limit>0`）**：语义/词法召回结合命中记忆的 source links，L3 可沿全部引用证据找到原文。长来源返回有说话人标记、至多 3200 字符的相关片段，而不是头部截断；原始存储不改写。能补回抽取未保留的细节，但增加 QA 上下文长度，不能把它当作等 token 预算比较。
- **抽取与归属**：区分用户/任务记录、信息请求、未来计划和助手提供的具体信息；保留原因、条件与对象指代。profile 支持单属性 `attribute/value` 形式；助手陈述不进入用户 profile。`MemoryItem.owner` 与 formatter 的 `[user]` / `[assistant]` / `[derived]` 保留直接陈述和推导的区别。
- **版本感知 System2**：当前事实可携带真正旧版本的历史上下文，用于变化原因/时间线，不把旧值提升为当前，也不把旧 L3 推导再当用户证据。每条 draft 只校验引用材料；生成温度为 0；推导日期来自证据，ID 包含内容/类型/证据，避免返回旧内容时碰撞旧 ID。提交只对当前 head 做 CAS，旧版本仍保留。
- **可选 cross-encoder**：标准 `/v1/rerank` 接口，设置 `rerank_endpoint` 后对候选统一重排；默认关闭，失败直接抛出、不重试。重排时分数来自同一 cross-encoder，包括 raw 路，不再沿用其原始 0.5 缩放上限。
- **有界数据库资源**：新 HNSW 库使用 4 个线程；Kuzu 使用 4 个执行线程、128 MiB buffer pool、默认 64 GiB 最大映射（可通过构造参数增大）；关闭 VectorStore 同时关闭 Chroma 原生 client（上游引用计数保护同路径其它 client）。实测 384 核环境的多库进程可累积近 3 万线程；原 8 TiB/库地址预留也会让多库评估 mmap 失败。
- **条件性记忆**：S1 保留明确表达的体验评价、原因与情境；S2 推导有证据支持的驱动原则和适用边界，不把单一领域偏好直接推广到无证据的领域。这是统一生成/校验约束，不使用评测题目、选项或题型标签。实验中 LME 增益显著，但 PM 未达到预定 +10/+20pp，完整结果及泛化限制见 benchmark 变更日志。
- **验证**：445 passed / 1 skipped；有界线程设置下全套约 13 秒。Benchmark 结果与当前实验限制见既有 `exp/dual_mem_exp/benchmarks/CHAGNELOG.md`。

### 较早迭代记录（下列重试/复核/merge 行为已被上面的当前契约取代）

- **批内冲突一次性裁决，不再重裁**（2026-10-18）
  - 真实现象：LME 一个 session 的候选中，常有两条同时"取代"同一个当前 head（同一批抽取的两条派生事实、两个 session 汇报同一变更）。`_prepare_nodes` 会拒绝，于是整批重裁——而模型信息不变，重裁通常再撞一次，一次写入白付 2 份 JEV。
  - 现在 `_settle_target_claims` 在提交前一次扫过：同一 target 的多个取代按 `action` 置信度**留一条**，落选者原文写成独立记忆（内容不丢、旧 head 仍被取代）；指向"本批没产生记忆的 proposal"的悬空关系（RAW_ONLY/INVALID 导致的）直接剔除，剩余关系继续。
  - 一次内存扫描，**零额外模型调用**；实测 LME dual 34 次写入 0 次冲突报错（改前 13 小时内 33 次）。
- **删除 `ADD_MERGE` 动作（写路径不再做文本合并）**：合并要额外一次生成 + 一次无损校验，任何细节遗漏就整段失败（PM 实测约 5% 写入因此失败）。互补片段现在按 `ADD_SUPPLEMENT` 各自独立写入，主题/对象相同靠检索侧分组呈现。随之删除 `_generate_merges`、`collect_merged_timestamps`、`merged_timestamps` 字段与 `tests/test_reflector.py`。**回退**：如需恢复合并语义，从本 commit 前的 tag 取回 `_generate_merges` 与 `ADD_MERGE` 选项。
- **重写 System2（dual）：一批生成 → 一次校验 → 直接 append，不再走关系决策**（2026-10-18）
  - 根因：旧的 L3 派生记忆也要过一遍 System-1 的 `target` + `action` 决策，而 target 题要带**全部当前 head** 作为选项。LME 一个 scope 一百多个 head，每条派生记忆都重付一次；实测 LME dual 8.5 h 只跑完 2/36 题（system1 同期 8/36），其中 JEV 5.4k 次请求。
  - 现在：`System2Agent` 只做「按主题打包 facts → 一次 LLM 生成 → 一次 Jev 校验每条 draft → 直接作为独立 L3 节点 append」。派生记忆不取代 fact，选 target 是纯开销。重复的 draft 按 embedding 相似度（`s2_dedup_threshold`，默认 0.97）跳过。
  - 同一 scope 的批次按 topic 顺序串行；不同 scope 仍并发。
  - **实测**：4 条事实写入 + digest，system1 104 s / dual 115 s（旧的 dual 同场景要数分钟到数十分钟）。
- **删除全部"失败重试 / 降级保证"机制**。设计原则改为：宁可丢掉这一次推断，也不在 SDK 里堆重试，稳定性与延迟优先于覆盖率。
  - 删除 review 队列（`ReviewWorker`、`review_queue` 表、`pending_reviews()`、`needs_review` 字段与 `pending_review` 状态）。未定的关系（`NEED_CONTEXT`、多目标 `UNSURE`、低置信度）**当场**按独立 SUPPLEMENT 写入，之后不再回头复核。
  - 删除写侧降级：`degrade_to_supplement`、冲突计划的 `_degrade_conflicting`、锁内重裁。计划最多两轮（1 次规划 + 1 次带上轮拒绝原因的规划），仍失败就抛 `DecisionPlanError`。
  - 删除抽取重抽：`extract_coverage_retries` 去掉，抽取只跑一次；coverage 被拒仍提交候选，只打一条 warning。`REEXTRACT` 复核路径随之删除。
  - 删除 S2 失败表与二分：`s2_failures` 表、`record_s2_failure` / `quarantined_s2_facts` / `list_s2_failures`、`s2_max_attempts`。批次失败只记日志并丢弃；这些 fact 的 `s2_evidence_count` 仍为 0，下一次 `digest()` 自然会重新处理，不需要任何簿记。
  - 配置项移除：`reconcile_retry_attempts`、`extract_coverage_retries`、`review_max_attempts`、`s2_max_attempts`。**旧的 `~/.dual_mem/config.yaml` 含这些键会因 `extra="forbid"` 直接报错**，删除这四行即可（本机已改，备份为 `config.yaml.bak-1018`）。
  - 新增 `s2_dedup_threshold`（默认 0.97）。
  - 行为变化：`WriteResult.status` 不再有 `pending_review`；`MemoryDecision.review_reason`、`WriteResult.needs_review`、`MemoryItem.needs_review` 都已从模型与检索结果里删掉。REST schema 同步。
- **测试**：按新契约重写 `tests/test_system2_agent.py`（原 review/二分/隔离用例全部替换为"失败即丢弃"的断言）；`tests/test_review_regressions.py` 删除 review 队列相关 8 个用例。全套 435 passed / 1 skipped。
- **merge 生成漏条目时逐条降级**（2026-10-17 PM/LME 实验：7/237 次写入因此整段失败）：生成模型漏返回某个 `proposal_id`、返回空文本或只返回未知 id 时，只把这些 merge 写成原文 `SUPPLEMENT` 并标 `needs_review`（`merge generation omitted proposal`），其余 merge 照常做无损校验。原先抛 `merge generation lacks source coverage` / `returned empty content`，丢整段 source。未生成的 merge 不再花一次 JEV 校验。
- **同 scope 并发写入的 CAS 冲突在 SDK 内重试**：`reconcile_retry_attempts` 次都输给并发提交后，持 scope 锁再完整重裁最多 2 次（`_STALE_LOCKED_RETRIES`），仍失败才抛 `StaleDecisionError`（REST 409，语义不变）。LME 同一用户 2 路 session 并发灌入时，`targets changed concurrently` 原先约占写入的 2%。
- **测试**：`tests/conftest.py` 每个测试后释放 chromadb 进程级 `SharedSystemClient` 缓存。原先一次跑全套会在 ~48% 卡死在 chromadb Rust `Bindings(...)`（几百个未关闭的 PersistentClient 累积；单文件都能过）。现全套一次跑：458 passed / 1 skipped，约 53 s。
- **决策无法执行时降级，不丢整段 source**（2026-10-17，PM/LME benchmark 真实 Venus jev-1.13.0 发现）：
  - 冒烟中 3/8 个 PersonaMem chunk 写入整段失败（`multiple operations consume one target`、`Jev rejected lossy merge`），session 的全部记忆丢失。
  - 带反馈重裁 `reconcile_retry_attempts` 次后仍冲突：每个 target 保留**首个**决策，后续撞车的 proposal 原文写成独立 `SUPPLEMENT` 并标 `needs_review`（`_degrade_conflicting`），由 `digest()` 复核。
  - JEV 判 merge 有损（`INVALID`）：该 proposal 原文按 SUPPLEMENT 写入、旧 head 保持 current、关系入复核队列；原先抛 `DecisionContextError`。
  - MERGE 依赖的批内 proposal 最终没产生记忆：该 merge 降级为 SUPPLEMENT（原先 `DecisionPlanError`）。
  - 共用 `degrade_to_supplement()`。**效果**：冒烟 PM 题 ingest 失败 3/8 → 0/8。**回退**：无开关；冲突仍会先按反馈重裁。
- **超长 source 截断而不报错**：source（决策上下文，不是存储材料）超过 `jev_max_question_chars` 预算时保留首尾，coverage 题跳过（部分原文无法判覆盖）。原先抛 `source and proposals exceed the Jev context budget`，LME 长 session / PM 12-pair chunk 直接写入失败。proposals 本身超预算仍报错。
- **自部署 System One 兼容**：choice 答案缺 `confidence` 时取 `probabilities[choice]`（train-jev `trainjev.serve` 只返回 choice + probabilities，原先每次写入都校验失败）。
- ⚠️ 已知：train-jev jev-4b v0.2.0 不适合作写侧决策器——蒸馏语料（jev-distill-corpus-v3，655k 行）没有任何 memory-write choice 样本，8 条明确事实只落库 1 条（Venus jev-1.13.0 8/8）。实验用 Venus。
- ⚠️ Venus System One：单请求 JSON > ~64k 字符直接 422（`System One 请求校验失败`），单请求 150 题正常；限 30 QPM。`jev_max_request_chars` 默认 100k 会超，Venus 上需 ≤ 62k。
- **写入降级而不丢弃**：coverage 拒绝 / 空抽取被判漏抽时带反馈重抽（`extract_coverage_retries`，默认 1），用尽后仍提交候选并把 L0 来源排队复核；`NEED_CONTEXT`、多目标 `UNSURE`、低于 `reconcile_review_confidence`（默认 0 = 关）的关系先按独立 SUPPLEMENT 写入并标 `needs_review`。`digest()` 在 system1 / dual 都先跑复核，失败项 `review_max_attempts` 次后隔离。**效果**：benchmark 不再因单个 session 的决策失败丢整段记忆。**速度**：coverage 失败时多一次生成 + JEV。**回退**：`extract_coverage_retries=0`。
- **乱序灌入**：来源 `memory_at` 早于 target 时只能选 `ADD_HISTORICAL` 等不退役 head 的动作；旧报告作为非 latest 版本插到 head 后面。**效果**：并发 / 乱序 ingest 不会让旧 session 覆盖新状态。
- **JEV 预算**：target 选项改为短标签，完整节点只在 state 里出现一次（原先每个 head 出现两次，约 48 个 head 就超 60k 字符）。head 超过 `reconcile_candidate_limit` 或材料超预算时，按 embedding 相似度排序截断，只决定比较对象，不做写入门；取代原来的全量扫描 + 逐页 INCLUDE/EXCLUDE。**速度**：大 scope 每次 add 的 JEV 调用从 O(N/64) 降为常数；路由 embedding 写入时复用。
- `MemoryDecision.action` 原样返回 `RAW_ONLY` / `INVALID`，不再压成 `SKIP`；`WriteResult` 新增 `status`（committed / known / raw_only / pending_review）与 `needs_review`。
- 新增 `dual_mem.errors`，顶层导出 `DecisionContextError` / `DecisionPlanError` / `CoverageError` / `StaleDecisionError` / `IdempotencyConflictError`；REST 映射 409 / 422，`AddRequest` 支持 `request_id`，`AddResponse` 返回决策与状态。
- 抽取输出明确的 `valid_from` / `valid_until` / `temporal_scope`；`search(as_of=...)` 过滤过期的 TEMPORAL；命中附带一跳 `previous` 与仍有效的 `conflicts`（`include_relations`，默认开）；formatter 渲染有效期、上一值与冲突。
- `search(raw_limit=N)` 新增 L0 原文兜底路（`SearchMemories.raw`，分数 ≤0.5），默认关闭。REST / MCP 返回的 `memories` 多一个 `raw` 键。
- System2：分批前按 embedding 最近邻链排序让同主题证据同批；新增 `kind=preference`（隐式偏好推断，带证据）；被拒批次同次 digest 内二分隔离，单条事实失败 `s2_max_attempts` 次后隔离，scope 不再因坏批次反复失败。
- 幂等结果移到独立 `request_outcomes` 表，按 `request_outcome_ttl_seconds`（默认 7 天）在 digest 时清理。
- 修复（review）：同一批中两条 `HISTORICAL` 挂到同一个 current head 不再被当作「重复消耗目标」而让整次写入失败；退役目标仍只能被消耗一次，同一 head 不能在一批里既被退役又挂历史版本。
- 修复（review）：来源早于 target 时不再提供 `ADD_MERGE`（MERGE 会退役 head）；乱序动作集只剩 `HISTORICAL` / `CONFLICT` / `SKIP` / `INVALID` / `NEED_CONTEXT`。
- 修复（review）：复核意图在版本计划提交**之前**入队，计划 journal 同时记录 `needs_review`。进程死在提交与返回之间时，复核项仍在队列；队列行丢失时，同 `request_id` 重试从 journal 重放结果并补回复核项，不重新裁决。复核遇到尚未落库的临时版本会保留重试（写入可能仍在进行），对应写入以其它计划提交后视为无需复核。重新入队会清零 attempts。
- 修复（review）：复核重新抽取使用稳定的 `review:{id}` 作为 request_id，提交后、`resolve_review` 前崩溃时，下次 digest 从 journal 重放，不会因裁决不同撞上已存在的版本 id 而被隔离。
- 修复（review）：tool 消息（coding）路径的 `add(request_id=...)` 现在幂等：原文 id 与记忆 id 由 request_id 派生，结果与发布在同一 SQLite 事务写入 `coding_requests`，重复提交直接重放，复用 id 但输入不同抛 `IdempotencyConflictError`；中途崩溃留下的未发布预留行会在重试时清除。
- 修复（review）：coverage / REEXTRACT 复核与原写入的竞争。复核项记录发起它的 `request_key`；`digest()` 只在该请求已完成后才重新抽取，未完成（仍在提交或已失败等待重试）时保留重试。原写入提交后重新打开该项（attempts 清零），避免并发 digest 期间累积的失败把它推进隔离。同一 `request_id` 的重试若覆盖已通过，复核视为不需要。原文没有产生任何版本的写入（REEXTRACT、全部 RAW_ONLY/SKIP）也在 journal 记一条空计划，重试从 journal 重放，复核也能看到它已完成。
- `versions.db` 新增 `requests` 表（request_key → plan），`completed_request` 不再顺序扫描整个 journal；已有库在打开时从 plans 补齐。
- coding 路径的 `request_id` 冲突改抛 `IdempotencyConflictError`（REST 409，原先为 500）。
- coding 重放记录只写调用方显式传入 `request_id` 的请求（自动生成的 id 无法重放，写进去只会白占空间），并加 `ts` 列，由 `digest()` 按 `request_outcome_ttl_seconds` 一并清理。
- 默认 `storage_dir` 从 `./.dual_mem_data` 改为 `./.dual_mem_v4`。未显式指定目录时不再打开旧目录，旧数据不迁移。配置或参数仍指向旧目录时，打开照旧报 `unsupported memory schema` 且不改写。

## [0.1.3] — 2026-10-15

- Replace write-side semantic rules and similarity routing with batched JEV decisions.
- Keep four canonical layers and only system1 / dual modes; retain linked history.
- Remove legacy layers/statuses, deferred reconcile worker, nested config adapters and automatic migrations.
- Add per-attempt JEV latency and generation reasoning telemetry; hy3 smoke explicitly uses no_think.
- JEV target 选项从节点正文改为完整 `_context_node` JSON（layer、kind、revision、有效期、source_ids）。候选一多会先撞上 255 个选项或 `jev_max_question_chars`，抛 `DecisionContextError`，不会静默改成 SUPPLEMENT。action 说明把「另一次搬家」和「当前居住地变化」分开。
- 打开 `layer_schema_version != 4` 或非 `ACTIVE` 的目录直接报错且不改写。已有 `.dual_mem_data` 必须换新 `storage_dir`；旧 reconcile 队列不再排空。
- `llm_json_mode=true` 时，若 user 消息里没有单词 `json`，请求前会补上 `Reply with json.`。只写在 system 里不够：部分兼容网关只检查 input，会拒绝 `response_format=json_object`（System2 digest 原先只有 `Return {...}`）。
- JEV 关系裁决是 target-first，而且 action 集合双向闭合。有 target 只能选 `OVERRIDE` / `MERGE` / `NEGATE` / `CONFLICT` / `SKIP` / `DELETE` / `INVALID` / `NEED_CONTEXT`；无 target 只能选 `SUPPLEMENT` / `TEMPORAL` / `RAW_ONLY` / `INVALID` / `NEED_CONTEXT`。两段 instructions 分开，无 target 时不再提到 SKIP。`TEMPORAL` 和 `SUPPLEMENT` 不写 target，也不把 target 放进 CAS。动作和 target 不匹配抛 `DecisionContextError`，不重试；批内关系若指向最终 `RAW_ONLY` 的 earlier proposal，则作为依赖计划冲突重裁决。`DELETE` 只能指向已提交的 current head，不能归档尚未提交的 earlier proposal。`SKIP` 必须且只能有一个覆盖目标；多目标共同覆盖应生成可引用的 `MERGE`。主路径现在也严格校验注入 provider 的回答完整性、类型和选项。没有可选 target 时仍是一次 JEV；有可选 target 时多一次。coding 使用同一套两阶段，不再有 `:repair` 补问。
- `add()` 不再对长文本自动摘要。独立 `Summarizer` 已删除；`L3_DERIVED` 只由 dual 的 `digest()` 生成。
- 实验性 coding 写入与主路径使用同一套 JEV 动作和 target-first 约束。所有 target 关系都必须留在相同 workspace / branch / strict-session 边界，避免跨项目 `SKIP`、`CONFLICT` 或 `DELETE`；target choice 为 `NONE` / `MULTIPLE` 预留两个选项后，`CodingMemoryReconciler(candidate_limit=...)` 最大为 253。coding 的 `MULTIPLE` 表示需要逐 head 裁决，展开后必须唯一选中一个 head。自定义 `DecisionProvider` 只需实现协议声明的 `evaluate()`，请求大小限制由 provider 自己执行。`DELETE` 只归档当前 head，不另写一行。`SKIP` 必须指向当前 head。对外 `MemoryDecision` 带上真实 `targets`、`proposal_ids` 和 `update_type`。
- `list` / MCP `memory_list` / REST `GET /v1/memories/` / 集成工具增加 `include_history`（CLI 为 `--include-history`），默认仍只列当前版本。`update` 说明改为追加新版本，`delete` 说明改为归档。`digest` 只在 dual 生成 `L3_DERIVED`。来源标注从 `(来源: L1 …)` 改为 `(来源: L0 …)`。

本文件记录 **dual-mem SDK** 中会影响 **延迟、LLM 调用次数、召回/写入效果** 的变更。  
Benchmark 对比或线上行为异常时，优先查此表 + 对应 `Settings` / `config.default.yaml` 开关。

格式说明：

- **效果**：对准确率 / 召回 / 写入完整性的影响
- **速度**：对 ingest / search 耗时的影响
- **Trade-off**：刻意取舍
- **回退**：关闭或改回旧行为的配置

Benchmark 专用覆盖见 `exp/dual_mem_exp/benchmarks/backends/dual_mem.py`（与 SDK 默认值可能不同）。

---

## [JEV redesign · 2026-10-15 · branch]

- 四层 L0_RAW / L1_FACT / L2_PROFILE / L3_DERIVED；仅保留 system1、dual。
- JEV 批量 action/target/representation，已知与更新分开，六种 update_type。
  删除 novelty、weak similarity 路由、skip_llm、sync 与规则裁决分支。
- 所有 reconcile 追加并链接历史；update 返回新 ID，delete 归档，latest 默认读取。
- 默认并发 4，复用 HTTP；版本 CAS + 持久计划恢复 + 图投影待处理记录。
- System2 完整分页覆盖事实，processed evidence 与版本原子提交；来源更新使推导过期。
- 确定性生成 smoke 的真实 Venus JEV 最新四步裁决为 408–562 ms；真实 hy3 生成链
  四步写入为 12–28 秒，embedding 为确定性适配器。未做准确率或速度对比基准。
- JEV 的 target 选项携带完整节点 JSON，而不只是正文。材料过长或选项超过 255 时写入失败并保留 source，不降级成 SUPPLEMENT。
- 旧存储目录不能打开，也不会被迁移。换新的 `storage_dir`。
- 兼容性详见 docs/jev-memory-design.md。

## [Historical · pre-JEV notes]

> 下面是 JEV 重构前的未发版记录，不是当前 SDK 行为。其中的旧层名、`SHADOW`、Gate、`content_hash_dedup`、reconcile 队列和对应配置都已删除。

### 写入：Provider 先落 L1；读侧挂来源 id

- **`add_raw` / `distill`**：`add_raw` 只写 L1（embed，不 extract）。`distill` 对已有 L1 跑 extract，成功后 SHADOW 源 L1，不再写第二条 L1。`add()` 契约不变。
- **Hermes/OpenClaw `sync_turn`**：每轮先 `add_raw`；满 `write_turn_window`（默认 5）、闲置 `idle_timeout_sec`（默认 30s）、或 `on_session_end` / `shutdown` 再 `distill`。第 1 轮原话即可 `search_conversation`。
- **`MemoryItem.source_node_id`**：从 `custom` 带出。`format_memories` / `format_memories_for_prompt` 渲染 `(来源: L1 <8 位>)`。`get(L1 id)` 仍可读 SHADOW 原文。
- **效果**：宿主短会话不会等满 5 轮才有原文；模型能从 fact 跳回 L1。
- **回退**：`write_turn_window=1` 恢复每轮 extract；`idle_timeout_sec=0` 关闲置。

### 写入：注入隔离、SKIP 收回、原文检索、主题目录

- **回写剥离注入块**：`strip_memory_injection` 在 `client.add` 去掉 `<relevant-memories>` / `<user-profile>` / `<memory-tools-guide>` / `<topic-catalog>`。只剩注入块的 turn 不写 L1。避免 prefetch 被再抽成新记忆。
- **空 updates = SKIP**：reconcile LLM 明确输出 `{"updates": []}` 或 `[]` 时，整批新记忆视为无增量。dual 路径把对应 fast-write 标 `SHADOW`；解析失败仍 fail-open（空 ops，不收回）。`non_destructive` 不收回。也可显式 `op=SKIP`（`memory_id` / `new_index`）。
- **`search_conversation`**：只搜 L1_RAW（含提取后 SHADOW），不混进默认 QA `search`。REST `/v1/memories/search_conversation`、MCP/CLI/`conversation_search` 工具。与 `memory_search` 共享每轮 3 次上限。
- **稳定主题目录**：`load_profile_block` 在 L0 后追加 `<topic-catalog>`（ACTIVE L2/L4 tags + L6 标题，排序、与 query 无关）。
- **效果**：记忆不再自我增殖；重复 fast-write 可被收回；模型能核原话；system 尾部有可缓存的主题索引。
- **回退**：无开关。默认 `search` 仍不召回 L1。

### 写入：`update_type=MERGE` + 血缘 / 注入拆分

- **互补合并不再靠 prompt**：`fold_absorb_deletes` 把旧 Objective 2 的 `ADD + DELETE` 收成 `MERGE` + `supersedes` 链（旧节点 `SUPERSEDED`，不是 `SHADOW`）。`conservative` / `non_destructive` 不走这条改写。读侧 MERGE 链默认展开为 `(folded, still valid)`；OVERRIDE 仍隐藏旧值。
- **仅 MERGE** 写入 `custom.merged_timestamps`（新旧 `memory_at` 并集）。`format_memories` 在长度 > 1 时渲染 `(持续: YYYY-MM-DD → YYYY-MM-DD)`；OVERRIDE 替换链不画「持续」，避免把 Java→Python 说成从 Java 年代开始用 Python。
- **dual fast-write** 给 L2/L4 打 `custom.source_node_id`（指向源 L1）；System1 reconcile 路径原本已有。
- **integrations**：`load_profile_block` / `format_profile_block` 按 L0 list（与 query 无关）；`prefetch` 只注入 `memories.normal`。`system_prompt_block` 追加 Provider 内工具指南 + 缓存的 profile。`prefetch` 默认 3000ms 超时；`memory_search` 每轮最多 3 次，超限返回 `limit_reached`。
- **效果**：互补合并可沿演化链回溯；prefix cache 只在宿主把 `system_prompt_block` 放 system、`prefetch` 放 user 时生效。
- **回退**：prompt / `update_type` 无开关；`non_destructive` 仍剥离全部 `supersedes`（含 MERGE）。

### 新增：原生生态集成层（`dual_mem.integrations`）

- 新增 `dual_mem.integrations` 包，把 `MemoryClient` 适配到 5 个生态后端：`mcp`、`hermes`、`openclaw`、`agentica`、`claude_code`。
- 统一 `MemoryBackend`（MemoryClient 薄封装）+ `AsyncRunner`（同步 hook 驱动异步 client）+ `<relevant-memories>` 注入块格式化。
- 新增 `agentica` extra（`dual_mem.integrations.agentica`）与 `dual-mem-hook` 脚本（Claude Code `search` / `ingest` 子命令）。
- 纯新增对外接口，不改变现有 ingest / search 路径的 LLM 调用次数与延迟。

### 新增：coding 记忆子系统（`dual_mem.coding`，实验性）

- 针对含工具调用的工程对话，独立 extractor / judge / preproc / reconciler / writer / store 路径。
- 未接入 `dual_mem.__all__` 公共导出、无对应测试，标记实验性；稳定后再纳入 README 主推。

### Gate 子系统已整体移除（本批 · commit 484f221）

- `dual_mem/agent/gate.py` 与 `tests/test_gate.py` 删除；提交决策完全由 **Extractor 单次 LLM 输出**（`is_ephemeral` + 结构化记忆）驱动，不再有独立的 Gate LLM 调用。
- **LLM 调用次数**：每个 `add` 现在**固定 1 次 extract LLM**。旧 combined 模式下 PASS 也是 1 次；但旧 REJECT short-circuit 可 0 次，新模型 REJECT/ephemeral 仍付 1 次 extract。
- **已删除配置**：`combined_gate_extract`、`gate_heuristic_shortcircuit`、`gate_shortcircuit_novelty`、`gate_shortcircuit_relevance`、`gate_shortcircuit_reject_novelty`、`embed_merge_l1_gate`（含 `config.default.yaml` 对应段）。
- **公开 API 兼容**：`WriteResult.gate_passed` 字段保留；内部 `GateResult` 重命名为 `CommitResult`（`GateResult = CommitResult` 别名兼容）。
- **Reconsolidation**：不再做情绪/arousal 推断，仅刷新 `last_reactivated_at`（零 LLM）。
- **不可回退**：Gate 已删，无配置可切回旧行为；如需低价值内容跳过 extract LLM，需另加本地启发式预筛（TODO）。

### 写入路径（ingest）— LLM 次数与并发

| 变更 | 配置 | 速度 | 效果 | Trade-off / 回退 |
|------|------|------|------|------------------|
| **Gate 子系统整体移除** | 无（配置已删） | 每 `add` **固定 1 次 extract LLM**（无独立 gate LLM；旧 REJECT short-circuit 可 0 次，现 REJECT/ephemeral 仍付 1 次 extract） | 提交决策由 Extractor 输出（`is_ephemeral` + 结构化记忆）驱动 | 不可回退（Gate 已删）；低价值内容省 extract 需另加本地预筛（TODO） |
| **Extract ∥ Summary 投机并发** | `summarizer_enabled: true` 且 `len(content) ≥ summarizer_min_content_length` | PASS turn：`summary` 与 `extract` **并行**（≈ `max(T_extract, T_summary)`） | REJECT / ephemeral turn 可能 **白跑 1 次 summary LLM**（~5% REJECT × 长文本） | Summary 失败 **不阻断** L0/L2/L4（`try/except` 后 `summary=None`）。cancel 打 `summary cancelled after …` 日志。Benchmark 已 `summarizer_enabled: false`。回退：关 summary |
| **Summary 仅在 extract 产出持久化记忆后落库** | （逻辑，无单独开关） | 避免 ephemeral / 无结构化记忆 turn 写 L3 | — | 与上条配合；REJECT/ephemeral 时 `_cancel_summarize_task` |
| **Summarizer 阈值提高** | `summarizer_min_content_length: 1500`（原 500 → 800 → 1500） | 更少 summary LLM | 更短对话无 L3 | Benchmark：`summarizer_enabled: false` |
| **Extract 长输入：中间截断** | `extract_max_content_chars: 0`（SDK 默认不截断）；benchmark `10000` | 20k+ 字符 **避免 50–80s outlier** | **丢失中间段落信息**（head+tail 保留） | 旧行为：靠 `llm_context_window` **分块多次 LLM + merge**（见下「LLM 分块合并」）。截断 vs 分块：**截断 = 1 次 LLM 更快但丢中间；分块 = N 次 LLM 更慢但更全**。回退：`extract_max_content_chars: 0` 且依赖 chunk merge |
| **Extract JSON 失败 retry** | `extract_retry_on_failure: true` | 解析失败时 **+1 次 LLM**（~20s） | 降低「整 turn 22 条 fact 全丢」 | retry 用 `temperature=0.0` + **JSON-only 强化 prompt**（不强制 `json_object`，兼容 vLLM）。回退：`extract_retry_on_failure: false` |
| **多轮 messages 输入整形** | `extract_history_context_ratio: 0.7`，`extract_assistant_max_tokens: 200` | 仅当整段对话占用 > `0.7 × llm_context_window`（token 粒度）才截 assistant，长会话 prompt token 下降 | **不丢任何轮**（40 轮提交得 40 轮全在）；**user 全保留**；超阈值时 assistant（AI 的话）截到 200 token；短对话原样透传 | 仅作用于 `messages=`；`content=` 不受影响。回退：ratio 或 tokens 设 `0` |
| **配置单位统一为 token** | `summarizer_min_content_tokens: 600`、`extract_max_content_tokens: 0`、`extract_assistant_max_tokens: 200`、`extract_history_context_ratio: 0.7` | — | 全部阈值统一 token 粒度，内部 `× chars_per_token` 转字符比较。`extract_max_content_tokens`（原 `_chars`）= 最终输入硬上限，与历史整形**分层叠加不冲突** | 改名（删除旧 `_chars`/`_length`/`shape_threshold_ratio`，不兼容旧名）。benchmark：`extract_max_content_tokens: 4000` |
| **Content-hash 写路径去重** | `content_hash_dedup: true`，`content_hash_scope: session\|user` | 相同 content **跳过 extract/reconcile** | session scope：跨 session 不误命中；user scope：跨 session 命中率更高 | `session`（默认）按 `app/user/agent/session`；`user` 按 `app/user`。回退：`content_hash_dedup: false` |
| **L1 embedding 合并 gate 开关已移除** | 无（配置已删） | L1 节点 embedding 现由 `memory_writer` 统一 `embed_queued` | — | 旧 `embed_merge_l1_gate` 已移除，无此开关 |
| **Reconciler embed 批量化** | （内部） | reconcile 候选 embedding **batch + 并发 query** | — | — |
| **L0/L2/L4 + L3/L7 embed 合并** | （内部 `embed_batch`） | summary + intention 一次 embed | — | — |
| **演化链 supersede 原子化** | （内部 `VectorStore.mark_superseded`） | — | 锁内 `get→改 superseded_by/is_latest/status→update payload` 一次完成，**不重写 embedding**；并发 supersede 不丢更新 | 替换原 `get→改→upsert`（mem_agent / reconciler_worker / basic_profile 三处统一）。无配置开关 |

### 读取路径（search）— 延迟与排序

| 变更 | 配置 | 速度 | 效果 | Trade-off / 回退 |
|------|------|------|------|------------------|
| **Hybrid 五路 anchor `to_thread`** | `reader_mode: hybrid` | 阻塞 store IO 不卡 event loop | — | 回退：`reader_mode: legacy` |
| **Entity 路径：子串扫描 → BM25** | hybrid 内置 | 200 条候选池内 BM25，替代全表 substring | 关键词命中更准 | 仍限 `entity_pool_limit` 候选池大小 |
| **Entity BM25 分数映射** | （内部） | — | fusion 中 entity 不再 **全部 ~0.9** 压过 vector 0.4–0.7 | `score = 0.25 + 0.45 * bm25_norm`（`bm25_norm` 已 max-normalize）。旧：`min(0.9, 0.3+0.6*raw)` |
| **Fusion access 批量读** | （内部 `get_access_batch`） | 少 N 次 SQLite  round-trip | — | — |
| **Query understanding 只算一次** | （内部） | 避免 hybrid 路径重复 `understand()` | — | — |
| **池内 BM25 置信度校准** | （内部） | 无额外存储查询 | BM25 分数同时考虑 query 覆盖率和候选池词项稀有度；精确稀有词仍可救回弱语义命中，但池内普遍词不再因 max-normalize 自动拿满分并穿过 `min_score` | 不引入 Trigger 模型或额外 LLM；部分词匹配的提升幅度低于旧行为 |
| **Reconsolidation 限频** | `reconsolidation_min_interval_sec: 0`（SDK）；benchmark `60` | 同 user 搜索 **减少 hook 开销** | dual 模式 S2 reconsolidation 任务更稀疏 | 回退：`reconsolidation_enabled: false` 或 `min_interval_sec: 0` |
| **Reconsolidation 仅刷新时间戳（零 LLM）** | （内部 `_run_reconsolidation`） | 无额外 LLM | dual：召回后仅刷新 `last_reactivated_at`，**不再做情绪/arousal 推断、不再置 `custom.reactivation`**（Gate 移除后去此逻辑） | 非 ReAct，按设计轻量 |
| **Hybrid 按 `target_layers` 路由** | （内部，`reader_mode: hybrid`） | — | QU 建议层 ∪ 常驻 profile 层下传 anchor（旧文档误标"写死层列表"） | 回退：`reader_mode: legacy` |
| **Evolution chain 批量 `get_by_ids`** | （内部） | 链展开少 N 次 `vector.get` | — | — |

### Embed / LLM 基础设施（v0.1.2 后持续）

| 变更 | 配置 | 速度 | 效果 | Trade-off / 回退 |
|------|------|------|------|------------------|
| **LLM 超长 prompt：分块 + merge** | `llm_context_window`，`llm_completion_reserve`，`chars_per_token` | 超预算时 **N 次 LLM** 再 merge | 比硬截断 **更全** 但更慢 | Extract **额外** 可用 `extract_max_content_chars` **强制截断**（优先速度）。Gate / Reconcile 等仍走 chunk merge |
| **Embed 超长文本：分块 mean-pool** | `embed_max_tokens`，`chars_per_token` | 长文本 embed 正确性 | — | — |
| **Embed 内存 cache 扩大** | `embed_cache_size: 10000`（SDK）；benchmark `200000` | 重复文本 embed **命中缓存** | 内存占用 ↑ | 回退：减小 `embed_cache_size` |
| **Embed 写入侧攒批** | `embed_queue_batch_size: 32`，`embed_queue_window_ms: 200` | 并发 write 合并 embed 请求 | search 侧仍直接 `embed` | — |

### Benchmark backend 与 SDK 默认值差异

下面这段是 JEV 重构前的 benchmark 覆盖。这些键已经从 `Settings` 删除，不要抄进当前配置：

```yaml
summarizer_enabled: false
extract_max_content_chars: 10000
content_hash_dedup: true          # session 级，PM/LME 命中率≈0
embed_cache_size: 200000
reconsolidation_min_interval_sec: 60
system2_trigger_mode: manual
persist_history: false
llm_json_mode: false              # vLLM Qwen 兼容
```

---

## 行为演进时间线（便于 git bisect）

| 时期 | 主题 | 代表 commit / 说明 |
|------|------|-------------------|
| v0.1.1 | Gate LLM 主路径、hybrid 读 | 基线：gate 与 extract **各 1 次 LLM** |
| fd63908 | 写侧 perf 第一批 | JSON mode、去 reconcile search-query、System2 多轮 |
| 07f6b4c | LLM/embed **分块 merge** | `chat_json_for_content` / `chat_text_for_content` chunk+merge |
| dcc8ae7 | P0/P1 ingest 优化包 | combined gate+extract、short-circuit、extract 截断/retry、summarizer 阈值、content_hash、读路径 BM25/to_thread |
| dc09d76 | P0 修复 | gate PASS 后再 summary（曾引入 extract∥summary 串行回归）、content_hash 加 session、BM25 entity 分数 |
| 47650c6 | 并发回收 | extract 前 **投机启动 summary**，REJECT/ephemeral **cancel** |
| 本批 | P0/P1/P2 收尾 + 历史整形 | messages 整形（assistant 截 500/最近 20 轮）、`content_hash_scope`、supersede 原子化、extract retry `temperature=0`+JSON-only、REJECT short-circuit、Kuzu `close()` 接入 `aclose()`；订正 target_layers / reconsolidation 过时文档 |
| 484f221 | Gate 子系统移除 | 删除 `gate.py`/`test_gate.py`；提交决策改由 Extractor 驱动；移除 `combined_gate_extract`/`gate_*`/`embed_merge_l1_gate` 配置；Reconsolidation 去情绪推断；`GateResult`→`CommitResult` |

---

## 快速回退清单（当前 SDK）

| 现象 | 优先尝试 |
|------|----------|
| 打开存储报 `unsupported memory schema` | 换新 `storage_dir`。旧库不迁移、不改写，队列里的旧 reconcile 任务不会再跑 |
| `DecisionContextError`，或 JEV 报选项/字符超预算 | 把这次 source 拆成更小的写入。上限是 `jev_max_questions`、`jev_max_question_chars`，每个 target choice 最多 255 个选项 |
| 写入变慢 | 看 usage 回调里每次 JEV 的 `latency_ms`。默认并发 4 |
| 生成 JSON 解析失败又重试 | `extract_retry_on_failure: false` |
| vLLM 400 on json_mode | `llm_json_mode: false` |

---

## [0.1.2] — 2026-06

- 初始对外版本号；含 dual/system1 模式、hybrid 读路径、Gate LLM、System2 ReAct。
- 详见 git tag `v0.1.2` 与 `docs/architecture.md`。

## [0.1.1] — 2026-06

- 首个 dual-system memory SDK 发布（Gate + Extract + 快写 + 异步 reconcile）。

---

维护约定：任何 **默认配置变更**、**LLM / JEV 调用次数变更**、**截断或上下文预算变更**、**并发变更**，合并时写入文首 `Unreleased`。
