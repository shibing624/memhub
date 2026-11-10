# 快速开始

## 同步（脚本 / REPL，推荐入门）

```python
from memhub import SyncMemoryClient

with SyncMemoryClient(mode="system1", storage_dir="./.memhub_data") as client:
    client.add(
        content="我最爱的编程语言是 Java，已经用了5年。",
        user_id="alice",
    )
    res = client.search(
        query="用户的编程语言偏好",
        user_id="alice",
    )
    for m in res.memories.profile:
        print(m.content, m.evolution_chain)
```

`SyncMemoryClient` 在后台线程跑 event loop，**复用同一实例**时性能与 `MemoryClient` 一致；不要用 `asyncio.run()` 包每一次调用。

## 异步（FastAPI / asyncio Agent）

```python
import asyncio
from memhub import MemoryClient


async def main():
    client = MemoryClient(mode="system1", storage_dir="./.memhub_data")
    await client.add(content="...", user_id="alice")
    await client.aclose()


asyncio.run(main())
```

已在 async 事件循环内时，必须用 `MemoryClient` + `await`；`SyncMemoryClient` 会报错。

配置：`~/.memhub/config.yaml` 首次启动自动创建，填入 `jev_api_key`、`llm_api_key` 与 `embed_api_key` 即可。

## 核心参数

### `MemoryClient(...)` 构造

| 参数 | 必填 | 含义 |
|---|---|---|
| `mode` | 否 | `"system1"`（默认，写 L0/L1/L2）或 `"dual"`（+ 显式 `digest()` 写 L3_DERIVED）。可写在 YAML。 |
| `storage_dir` | 否 | 本地数据目录（Chroma + Kuzu + SQLite），默认 `./.memhub_data`。 |
| `settings` | 否 | 传入 `Settings` 对象，覆盖 YAML / 环境变量。 |
| `embed` / `llm` / `jev` | 否 | 注入自定义客户端（测试 / 自建后端）；注入后跳过对应密钥校验。 |

### `add(...)` / `search(...)` 归属标识

| 参数 | 必填 | 含义 |
|---|---|---|
| `user_id` | **是** | 终端用户 ID，读写必须一致才能命中。 |
| `app_id` | 否 | 省略时用 `settings.default_app_id`（默认 `"default"`）。多产品共享同一服务时可显式传入。 |
| `app_ids` | search 否 | 省略时用 `[default_app_id]`。 |
| `agent_id` | 否 | 同一用户下细分 Agent 实例（多 Bot）。 |
| `session_id` | 否 | 会话 ID，可选更细隔离或审计。 |

配置项 `default_app_id`（YAML / `MEMHUB_DEFAULT_APP_ID`）可改默认命名空间。

### `content` vs `messages`

同一条 `add()` API，入参形式不同：

| 方式 | 适用 | 行为 |
|---|---|---|
| `content="..."` | 单条陈述、日志、离线导入 | 整段由 生成候选文本，再由 JEV 决定是否沉淀。 |
| `messages=[{role, content}, ...]` | Agent 多轮对话（推荐） | L0_RAW 与 Extractor 都接收带角色标记的完整对话；宿主注入的 system 消息会先移除。 |

```python
await client.add(
    messages=[
        {"role": "user", "content": "我住在上海"},
        {"role": "assistant", "content": "好的，记住了"},
        {"role": "user", "content": "下个月要搬去北京工作"},
    ],
    user_id="alice",
)
```

## Coding 记忆（工程排障经验）

chat 和 coding 用同一个 `add()` / `search()`，不需要开关。`add(messages=...)` 里出现工具轨迹
（assistant 的 `tool_calls` 或 `role="tool"` 结果）时，JEV 判断整段是不是工程会话：

- **是**：照常抽事实和画像，同时另外抽出可复用的工程经验，存成 `kind="procedure"` 的 L1 记忆。
  两路抽取并行，结果进同一次写入。
- **否**：去掉工具消息后按普通对话写入。

没有工具消息的写入完全不受影响，chat 的 prompt、裁决和召回与此前逐字节一致。

```python
import json
from memhub import MemoryClient

client = MemoryClient()

result = await client.add(
    messages=[
        {"role": "user", "content": "app/db.py 高并发写入偶发 database is locked"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_1",
                "type": "function",
                "function": {"name": "read_file", "arguments": json.dumps({"path": "app/db.py"})},
            }],
        },
        {"role": "tool", "tool_call_id": "call_1", "content": "conn = sqlite3.connect(db_path, timeout=5.0)"},
        {"role": "assistant", "content": "写事务改为 BEGIN IMMEDIATE，timeout 调到 30s。"},
    ],
    user_id="alice",
    workspace_id="~/code/app",   # 可选：procedure 的版本边界
    branch="main",
)
print(result.scene, result.procedure_ids)   # "coding", [新 procedure 版本 id]

res = await client.search(
    query="SQLite database is locked 怎么解决",
    user_id="alice",
    procedure_limit=3,          # 默认 0 = 不返回 procedures 组
)
for m in res.memories.procedures:
    p = m.procedure
    print(p.task, "->", p.solution, p.files, m.evolution_chain)
```

- **消息格式**：OpenAI 风格。`tool_calls` 支持嵌套 `function.name / function.arguments`
  （字符串或对象）和扁平 `name / arguments`；`files` 从 `path`、`file_path` 等路径参数结构化
  提取。工具输出（`role="tool"`）不进模型、也不进 L0；L0_RAW、procedure 抽取和 JEV 裁决都只看
  用户的请求和 assistant 的最终回答。对话超过 `procedure_source_max_chars`（默认 24000）时，
  优先完整保留用户消息，assistant 回答按剩余预算截短（保留头尾）；裁决材料若仍超出
  `jev_max_question_chars`，则从最早的旧 head 开始丢弃，确保长会话不超出 JEV 预算。
- **一条经验**（`MemoryItem.procedure`）：`task`（检索问题）、`solution`、`search_keys`、
  `boundary_envs`、`boundary_scope`（`strict` / `project` / `user` / `global`）、`files`、
  `workspace_id`、`branch`。`content` 是 task + solution 的可读文本；向量由 task 和
  `search_keys` 拼接后计算（检索时匹配的是「问题」一侧，而不是方案正文）。
- **存储与隔离**：和对话记忆同一个向量库、同一个 scope（`app_id / user_id / agent_id`）和同一条
  版本链。chat 的 profile / proactive / normal 组和 chat 写入裁决都看不到 procedure，
  两边分数不需要互相比较。
- **版本演化**：同一问题出现新方案时，JEV 判 OVERRIDE 追加新版本并退役旧版本；只是补充时判
  SUPPLEMENT，两条都保持现行（没有 MERGE：合并重写会丢掉旧方案里的约束）；
  检索只返回当前版本，`evolution_chain` 指向被替换的旧版本。只有同一版本边界
  （workspace / branch / boundary_scope，`strict` 还要求同一 session）内的经验之间才会建立关系。
  `update()` / `delete()` / `get()` / `list()` 对 procedure 同样适用。
- **裁决**：JEV 判 `INVALID`（候选与来源不符）只丢弃那一条候选，其它候选和 L0 来源照常保留；
  `NEED_CONTEXT` 写成独立版本。
- **dual**：procedure 写入同样登记 System 2 待处理 scope，`digest()` 可以从多次工程经验里
  归纳出习惯（例如「这个用户一贯先跑完整测试」）；人格目录（catalog）只引用对话事实。
- **代价**：带工具轨迹的写入 = 一次 scene 判断 + chat 抽取与 procedure 抽取（并行）+ 各自裁决。
  建议会话结束时整段写入，不要每轮调用。`procedure_enabled=False` 可整体关闭。
- **接口**：REST `POST /v1/memories/`（`workspace_id` / `branch`）与 `POST /v1/memories/search`
  （`procedure_limit` / `procedure_min_score`）、MCP 同名参数、CLI `memhub add --workspace-id`
  和 `memhub search --procedure-limit`。MemHub 采集 Claude Code / Codex / Agentica / CodeBuddy / OpenCode / WorkBuddy 会话时保留对话正文与工具调用涉及的文件路径（不读取工具输出）。

完整可运行示例（含方案演化）：[`examples/06_coding_memory.py`](https://github.com/shibing624/memhub/blob/main/examples/06_coding_memory.py)。

## Agent 集成（Agentica 多轮对话）

memhub 管**长期记忆**（跨会话），Agentica `WorkingMemory` 管**当前会话窗口**——两者配合，结构对齐 [Agentica `05_multi_turn.py`](https://github.com/shibing624/agentica/blob/main/examples/basic/05_multi_turn.py)：

- **每轮 `search`**：用用户本轮输入检索长期记忆，注入 `prompt_config.additional_context`
- **每轮 `agent.run`**：Agentica 自带 `add_history_to_context=True` 维护窗口
- **会话结束 `add(messages=...)`**：把 `working_memory.get_messages()` 批量写入 memhub

需已安装 Agentica（`pip install agentica`）并配置 LLM API key。

```python
import asyncio

from agentica import Agent, OpenAIChat
from memhub import MemoryClient
from memhub.retrieval.formatter import format_memories

APP_ID = "agentica"  # 可选；省略时用 default_app_id
USER_ID = "demo_user"


async def main() -> None:
    memory = MemoryClient(mode="system1")
    agent = Agent(
        model=OpenAIChat(id="gpt-4o-mini"),
        add_history_to_context=True,
        instructions="你是一个友好的助手，请记住用户告诉你的信息。",
    )
    try:
        user_turns = [
            "我最喜欢的颜色是蓝色",
            "我喜欢吃川菜",
            "总结一下你了解到的关于我的信息",
        ]
        for user_msg in user_turns:
            ctx = await memory.search(
                query=user_msg,
                user_id=USER_ID,
            )
            block = format_memories(ctx.memories.to_dict())
            agent.prompt_config.additional_context = (
                f"以下是与用户相关的长期记忆，回答时可参考：\n{block}" if block else None
            )
            reply = await agent.run(user_msg)
            print(f"User: {user_msg}")
            print(f"Assistant: {reply.content}\n")

        session_messages = agent.working_memory.get_messages()
        write = await memory.add(
            messages=session_messages,
            user_id=USER_ID,
        )
        print(
            f"Session persisted: memory_id={write.memory_id[:8]}…  "
            f"extracted={write.extracted_count}  commit={write.commit_passed}"
        )
    finally:
        await memory.aclose()


if __name__ == "__main__":
    asyncio.run(main())
```

| 操作 | 建议 |
|---|---|
| `search` | 每轮用户输入前。默认不返回 `L0_RAW`；核对原话用 `search_conversation` |
| `add` | 会话结束一次（或每 N 轮 batch）。正常一次是 1 次候选生成 + 1 次 JEV，长文本不会在 `add()` 里自动摘要 |
| `WriteResult` | `memory_id` 是 `L0_RAW`。`commit_passed=False` 表示没有追加结构化版本（如 `RAW_ONLY`），原文仍保留。`decisions` 带 action、update_type、targets |
| `update` / `delete` | `update` 返回新 id；`delete` 归档当前版本。列出历史用 `list(..., include_history=True)` |

## dual 模式与 System2 触发

dual 的 `add()` 和 system1 完全相同，返回前已完成 L1/L2（以及 procedure）写入；
额外只做一件事：把这个 scope 登记为待归纳。SDK 不启动后台任务，由应用在会话结束、
批量导入完成或其它明确边界调用 `await client.digest()`。同一 scope 不论 add 多少次，
一次 digest 只处理一遍。

开启方式（优先级从高到低）：`MemoryClient(mode="dual")`、`MEMHUB_MODE=dual`、
`~/.memhub/config.yaml` 里 `mode: dual`。开启之后 `add()` 只是把 scope 登记为待归纳，
不会自己跑。SDK 在边界上调用一次 `await client.digest()`；命令行用 `memhub digest`
手动跑一次。配置文件已经是 dual 时不用再加参数；配置仍是 system1 时，这次要写成
`memhub digest --mode dual`，否则按 system1 处理，`processed` 为 0。

MemHub 的 Core 在 `mode: dual` 时自己归纳，一天最多一次：写入队列排空后，距上次成功
已满 `--digest-every` 小时（默认 24），并且还有待归纳证据，才跑。`--digest-every 0`
关闭自动归纳。评测集用这个关掉，语料写完后自己调用 `digest()`。

```python
from memhub import MemoryClient
from memhub.config import Settings

client = MemoryClient(settings=Settings(mode="dual", storage_dir="./.memhub_data"))
# ... await client.add(...) 若干次 ...
digest = await client.digest()
await client.aclose()
```

更多示例见 [examples/](https://github.com/shibing624/memhub/tree/main/examples)。

## 两档模式怎么选

| | **system1**（默认） | **dual** |
|---|---|---|
| 记什么 | 原文、事实、画像（L0 / L1 / L2） | 同左，`digest()` 时再归纳跨证据规律（L3） |
| 适合 | 个人助手、客服、快速接入 | 需要行为画像、意图推断的深度场景 |
| 额外开销 | 无 | 显式调用 `digest()` 时的生成与校验 |

L3 推导记忆默认只写入、不进入检索结果（`derived_context_limit=0`）。

## REST / MCP / CLI 速查

```bash
pip install "memhub[all]"

memhub serve-api --host 0.0.0.0 --port 8000          # 独立多租户 REST 服务（本机个人 Hub 用 memhub serve）
memhub-mcp                                              # MCP stdio
memhub-mcp --transport streamable-http --port 8765     # MCP HTTP /mcp
memhub add --content "用户喜欢咖啡" --user-id u1
memhub search "饮品偏好" --user-id u1
memhub digest --mode dual                               # dual：生成 L3_DERIVED
```

REST 契约：`POST /v1/memories/`、`POST /v1/memories/search`、`GET|DELETE /v1/memories/{id}` 等，完整对照见 [MCP / REST](../mcp_integration.md)。

## 推荐配置

默认配置就是评测得到的最佳设置，通常不需要改。唯一值得按场景调整的是写入决策的调用阈值：

| 配置 | 默认 | 说明 |
|---|---|---|
| `reconcile_relation_threshold` | `0.6` | 新记忆与已有记忆的相似度达到阈值，才请决策模型判断"重复 / 更新 / 新增"；低于阈值直接新增 |
| `procedure_source_max_chars` | `24000` | coding 会话送入 procedure 抽取与 JEV 裁决的对话字符预算（优先保留用户消息） |
| `embed_chars_per_token` | `1.2` | Embedding 输入字符上限的换算系数（`embed_max_tokens × 1.2`）；另按 `embed_max_tokens` 个 UTF-8 字节封顶，兼容中文、emoji 与代码混排文本，防止超出上游 token 限制 |

在意决策模型费用时可以设成 `0.7`：调用次数少约 27%，写入时间少约 10%，评测上没有测到掉分。
