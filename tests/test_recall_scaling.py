"""Recall cost must not grow with re-reading the whole store on every query.

1. The inverted ``LexicalIndex`` scores exactly like ``compute_bm25_scores``.
2. The store's write generation changes on every write path, including another
   process writing the same store, and lexical recall sees new memories at once.
3. The project view is reused until the store changes.
4. ``doctor`` reports the store size and warns on a slow last recall.
"""

from __future__ import annotations

import json
import math
import random
from pathlib import Path

import httpx
from typer.testing import CliRunner

from memhub import MemoryClient
from memhub.cli import hub as cli_hub
from memhub.cli import main as cli_main
from memhub.config import Settings
from memhub.hub import HubClient
from memhub.hub.server import HUB_USER_ID, ProjectViewCache, create_hub_app
from memhub.retrieval.bm25 import LexicalIndex, compute_bm25_scores, tokenize
from memhub.storage.vector_store import ChromaVectorStore
from memhub.types import Layer, MemoryNode

runner = CliRunner()


def _node(i: int, text: str, dim: int = 8) -> MemoryNode:
    return MemoryNode(
        node_id=f"n{i}", content=text, layer=Layer.L1_FACT, app_id="a", user_id="u",
        embedding=[1.0] + [0.0] * (dim - 1),
    )


def test_lexical_index_scores_match_the_full_scan() -> None:
    rng = random.Random(7)
    vocab = ["uv", "pip", "部署", "端口", "8080", "redis", "缓存", "超时", "重试", "日志"]
    docs = [" ".join(rng.choices(vocab, k=rng.randint(1, 12))) for _ in range(300)]
    index = LexicalIndex(docs)
    for query in (["uv", "部署"], ["redis", "超时", "absent"], ["8080"]):
        full = compute_bm25_scores(query, docs)
        sparse = index.scores(query)
        assert {i for i, s in enumerate(full) if s > 0} == set(sparse)
        assert all(math.isclose(full[i], s, rel_tol=1e-12) for i, s in sparse.items())


def test_generation_changes_on_every_write_and_across_processes(tmp_path: Path) -> None:
    store = ChromaVectorStore(str(tmp_path))
    seen = [store.generation]
    store.upsert([_node(1, "端口 8080")])
    seen.append(store.generation)
    store.update_payload("n1", {"tags": "x"})
    seen.append(store.generation)
    # A second process opening the same directory writes; this one must notice.
    other = ChromaVectorStore(str(tmp_path))
    other.upsert([_node(2, "端口 9090")])
    seen.append(store.generation)
    store.delete(["n1"])
    seen.append(store.generation)
    assert seen == sorted(set(seen)), seen
    other.close()
    store.close()


async def test_lexical_recall_sees_a_new_memory_right_after_the_write(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "store"), mode="system1"),
        embed=fake_embed, llm=fake_llm, jev=fake_jev,
    )
    await core.add(content="部署脚本用 uv 运行", user_id="u", request_id="r1")
    first = await core.search(query="qx9zz 错误码", user_id="u", limit=5)
    assert not any("qx9zz" in m.content for m in first.memories.normal)
    await core.add(content="错误码前缀是 qx9zz", user_id="u", request_id="r2")
    second = await core.search(query="qx9zz 错误码", user_id="u", limit=5)
    assert any("qx9zz" in m.content for m in second.memories.normal)
    await core.aclose()


def test_project_view_is_reused_until_the_store_changes(monkeypatch) -> None:
    import memhub.hub.server as server

    calls: list[str] = []

    def build(_client, workspace):
        calls.append(workspace)
        return [f"s{len(calls)}"], None

    class Store:
        generation = 1

    class Client:
        factory = type("F", (), {"vector": Store})

    monkeypatch.setattr(server, "project_sessions_and_view", build)
    cache = ProjectViewCache(max_age=60)
    assert cache.get(Client, "/w") == (["s1"], None)
    assert cache.get(Client, "/w") == (["s1"], None)
    Store.generation = 2
    assert cache.get(Client, "/w") == (["s2"], None)
    assert cache.get(Client, "/other") == (["s3"], None)
    assert calls == ["/w", "/w", "/other"]
    expired = ProjectViewCache(max_age=0)
    expired.get(Client, "/w")
    expired.get(Client, "/w")
    assert calls[-2:] == ["/w", "/w"]


async def test_status_reports_the_last_recall_time(
    tmp_path: Path, fake_embed, fake_llm, fake_jev
) -> None:
    core = MemoryClient(
        settings=Settings(storage_dir=str(tmp_path / "store"), mode="system1"),
        embed=fake_embed, llm=fake_llm, jev=fake_jev,
    )
    app = create_hub_app(api_key="secret", hub_dir=tmp_path / "hub", client=core)
    async with app.router.lifespan_context(app):
        async with HubClient(
            base_url="http://hub", api_key="secret", transport=httpx.ASGITransport(app=app)
        ) as remote:
            assert (await remote.status())["recall"] is None
            await remote.recall(query="端口", workspace=str(tmp_path))
            last = (await remote.status())["recall"]
    assert last["seconds"] >= 0 and last["at"] > 0
    await core.aclose()


def test_doctor_warns_on_a_slow_recall(tmp_path: Path, monkeypatch) -> None:
    status = {
        "memories": {"current": 41_000},
        "writes": {"done": 1},
        "recall": {"at": 1, "seconds": cli_hub.RECALL_WARN_SECONDS + 0.5},
    }
    monkeypatch.setattr(cli_hub, "daemon_status", lambda _root: status)
    empty = tmp_path / "bin"
    empty.mkdir()
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PATH", str(empty))
    for key in ("JEV", "LLM", "EMBED"):
        monkeypatch.setenv(f"MEMHUB_{key}_API_KEY", "k")
    out = runner.invoke(cli_main.app, ["doctor", "--json", "--hub-dir", str(tmp_path / "hub")])
    check = {c["name"]: c for c in json.loads(out.output)["checks"]}["Recall latency"]
    assert check["status"] == "warn"
    assert check["detail"].startswith("41000 current memories; last recall 3.50s")
    status["recall"] = {"at": 1, "seconds": 0.4}
    out = runner.invoke(cli_main.app, ["doctor", "--json", "--hub-dir", str(tmp_path / "hub")])
    check = {c["name"]: c for c in json.loads(out.output)["checks"]}["Recall latency"]
    assert check["status"] == "ok"
