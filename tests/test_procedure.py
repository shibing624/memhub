"""Procedure memory (coding scene) in the main store, fully mocked.

A tool trajectory that JEV judges to be engineering work yields ``kind="procedure"``
L1 nodes next to the chat memories of the same conversation. These tests pin the
store shape, the decision protocol, the separate search group, idempotency and the
dual-mode digest coverage through the public ``MemoryClient``.
"""

import asyncio
import json

import httpx
import pytest
import respx
from conftest import FakeJev, FakeLLMClient

from memhub import MemoryClient
from memhub.coding.extractor import CodingMemoryExtractor
from memhub.coding.preproc import coding_dialogue, fit_dialogue
from memhub.coding.reconciler import CodingMemoryReconciler
from memhub.coding.types import (
    PROCEDURE_KIND,
    CodingDecisionError,
    CodingMemoryDraft,
    CodingReconcileOp,
)
from memhub.coding.writer import _settle
from memhub.config import Settings
from memhub.providers.llm import LLMClient
from memhub.types import Layer

CANDIDATE = {
    "task": "How to publish SQLite transactions safely",
    "solution": "Use BEGIN IMMEDIATE so the write lock is taken before validation",
    "search_keys": ["sqlite database is locked during commit"],
    "boundary_scope": "project",
}
OVERRIDE = {
    "task": "How to publish SQLite transactions safely",
    "solution": "Enable WAL mode; BEGIN IMMEDIATE alone still blocks readers",
    "search_keys": [],
}


def trajectory(result="BEGIN IMMEDIATE fixed the lock"):
    return [
        {"role": "system", "content": "Host secret instructions"},
        {"role": "user", "content": "I always use uv, never pip. Fix the SDK transaction"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "c1",
                    "type": "function",
                    "function": {
                        "name": "edit_file",
                        "arguments": json.dumps({"path": "memhub/store.py"}),
                    },
                }
            ],
        },
        {"role": "tool", "tool_call_id": "c1", "content": result},
        {"role": "assistant", "content": "Done: the publish path now locks first."},
    ]


def client(tmp_storage, fake_embed, jev, llm=None, **settings):
    return MemoryClient(
        settings=Settings(storage_dir=tmp_storage, **settings),
        embed=fake_embed,
        llm=llm or FakeLLMClient({"json": {"candidates": [CANDIDATE]}}),
        jev=jev,
    )


def procedure_calls(llm):
    return [c for c in llm.calls if "candidate procedure memories" in c["system"]]


def scene_calls(jev):
    return [c for c in jev.calls if "scene" in c["questions"]]


async def test_tool_trajectory_yields_chat_and_procedure_memories_from_one_raw(
    tmp_storage, fake_embed
):
    jev = FakeJev({"scene": "CODING", "0:action": "ADD_SUPPLEMENT"})
    llm = FakeLLMClient({"json": {"candidates": [CANDIDATE]}})
    c = client(tmp_storage, fake_embed, jev, llm)
    original = trajectory()
    r = await c.add(messages=original, user_id="u", workspace_id="/repo", branch="main")

    assert r.scene == "coding" and r.status == "committed" and r.commit_passed
    assert len(r.procedure_ids) == 1
    proc = c.factory.vector.get(r.procedure_ids[0])
    assert (proc.layer, proc.kind, proc.is_latest) == (Layer.L1_FACT, PROCEDURE_KIND, True)
    assert proc.custom["source_ids"] == [r.memory_id]
    assert proc.custom["procedure"]["workspace_id"] == "/repo"
    assert proc.custom["procedure"]["branch"] == "main"
    assert proc.custom["procedure"]["files"] == ["memhub/store.py"]
    assert proc.tags == ["memhub/store.py"]
    assert proc.content.startswith(CANDIDATE["task"])
    # The embedding is the question side (task + alternative phrasings).
    expected = await fake_embed.embed_batch(
        ["\n".join([CANDIDATE["task"], *CANDIDATE["search_keys"]])]
    )
    assert proc.embedding == pytest.approx(expected[0], abs=1e-6)

    # Chat memories of the same conversation are still extracted and cite the raw.
    chat_ids = [i for i in r.extra_node_ids if i not in r.procedure_ids]
    assert chat_ids
    chat_nodes = c.factory.vector.get_by_ids(chat_ids)
    assert all(n.kind != PROCEDURE_KIND for n in chat_nodes.values())
    assert all(n.custom["source_node_id"] == r.memory_id for n in chat_nodes.values())
    chat_extract = [c for c in llm.calls if c not in procedure_calls(llm)]
    assert "BEGIN IMMEDIATE" not in json.dumps([c["user"] for c in chat_extract])

    # L0_RAW keeps the dialogue: the request and the answer, no tool output,
    # no host system turn.
    raw = c.factory.vector.get(r.memory_id)
    assert raw.layer == Layer.L0_RAW
    assert "Fix the SDK transaction" in raw.content
    assert "the publish path now locks first" in raw.content
    assert "BEGIN IMMEDIATE fixed the lock" not in raw.content
    assert "edit_file" not in raw.content and "Host secret" not in raw.content
    assert original[0]["content"] == "Host secret instructions"  # caller data untouched
    assert len(procedure_calls(llm)) == 1 and len(scene_calls(jev)) == 1
    await c.aclose()


async def test_procedures_are_a_separate_search_group(tmp_storage, fake_embed):
    c = client(tmp_storage, fake_embed, FakeJev({"scene": "CODING"}))
    r = await c.add(messages=trajectory(), user_id="u")
    query = CANDIDATE["task"]

    off = await c.search(query=query, user_id="u", min_score=0.0)
    assert off.memories.procedures == []
    chat_ids = {
        m.memory_id
        for m in [*off.memories.normal, *off.memories.profile, *off.memories.proactive]
    }
    assert not chat_ids & set(r.procedure_ids)

    on = await c.search(query=query, user_id="u", min_score=0.0, procedure_limit=3)
    assert [m.memory_id for m in on.memories.procedures] == r.procedure_ids
    hit = on.memories.procedures[0]
    assert hit.kind == PROCEDURE_KIND
    assert hit.procedure.solution == CANDIDATE["solution"]
    assert hit.procedure.search_keys == CANDIDATE["search_keys"]
    assert on.to_dict()["memories"]["procedures"][0]["procedure"]["task"] == query
    # flatten() appends procedures after the bounded chat evidence.
    assert on.memories.flatten(limit=1)[-1].memory_id == r.procedure_ids[0]
    await c.aclose()


async def test_lexical_route_finds_exact_error_strings(tmp_storage, fake_embed):
    c = client(tmp_storage, fake_embed, FakeJev({"scene": "CODING"}))
    r = await c.add(messages=trajectory(), user_id="u")
    out = await c.search(
        query="BEGIN IMMEDIATE", user_id="u", procedure_limit=3, procedure_min_score=0.0
    )
    assert [m.memory_id for m in out.memories.procedures] == r.procedure_ids
    await c.aclose()


async def test_chat_scene_judgement_stores_no_procedure(tmp_storage, fake_embed):
    jev = FakeJev({"scene": "CHAT"})
    llm = FakeLLMClient({"json": {"candidates": [CANDIDATE]}})
    c = client(tmp_storage, fake_embed, jev, llm)
    r = await c.add(messages=trajectory(), user_id="u", request_id="r1")
    assert r.scene == "chat" and r.procedure_ids == [] and r.extra_node_ids
    assert procedure_calls(llm) == []
    assert c.factory.vector.get_many({"kind": PROCEDURE_KIND}) == []
    await c.aclose()


async def test_plain_chat_never_asks_the_scene_question(tmp_storage, fake_embed):
    jev = FakeJev()
    c = client(tmp_storage, fake_embed, jev)
    r = await c.add(messages=[{"role": "user", "content": "I like tea"}], user_id="u")
    assert r.scene == "chat" and scene_calls(jev) == []
    await c.aclose()


async def test_disabled_procedures_leave_tool_turns_to_the_chat_path(
    tmp_storage, fake_embed
):
    jev = FakeJev({"scene": "CODING"})
    llm = FakeLLMClient({"json": {"candidates": [CANDIDATE]}})
    c = client(tmp_storage, fake_embed, jev, llm, procedure_enabled=False)
    r = await c.add(messages=trajectory(), user_id="u")
    assert c.procedures is None
    assert r.scene == "chat" and scene_calls(jev) == [] and procedure_calls(llm) == []
    await c.aclose()


async def test_override_retires_head_and_search_bridges_to_current(
    tmp_storage, fake_embed
):
    jev = FakeJev({"scene": "CODING"})
    llm = FakeLLMClient({"json": {"candidates": [CANDIDATE]}})
    c = client(tmp_storage, fake_embed, jev, llm)
    head = (await c.add(messages=trajectory(), user_id="u")).procedure_ids[0]

    llm.responses["json"] = {"candidates": [OVERRIDE]}
    jev.responses = {"scene": "CODING", "0:target": head, "0:action": "ADD_OVERRIDE"}
    r = await c.add(messages=trajectory("WAL mode fixed it"), user_id="u")
    new = r.procedure_ids[0]
    old_node, new_node = c.factory.vector.get(head), c.factory.vector.get(new)
    assert not old_node.is_latest and old_node.superseded_by == [new]
    assert new_node.supersedes == [head] and new_node.custom["update_type"] == "OVERRIDE"
    assert r.decisions[-1].targets == [head]

    # The head's ancestors are shown to JEV on the next decision.
    jev.calls.clear()
    jev.responses = {"scene": "CODING", "0:target": "NONE"}
    await c.add(messages=trajectory(), user_id="u")
    current = [s for s in jev.calls if "0:target" in s["questions"]][0]["state"]["current"]
    by_id = {m["memory_id"]: m for m in current}
    assert head not in by_id
    assert [h["memory_id"] for h in by_id[new]["history_versions"]] == [head]

    out = await c.search(
        query=CANDIDATE["task"], user_id="u", procedure_limit=5, procedure_min_score=0.0
    )
    assert head not in [m.memory_id for m in out.memories.procedures]
    assert new in [m.memory_id for m in out.memories.procedures]
    await c.aclose()


async def test_supplement_to_a_head_keeps_both_current(tmp_storage, fake_embed):
    """An addition to a head is its own version; the head keeps its constraints."""
    jev = FakeJev({"scene": "CODING"})
    llm = FakeLLMClient({"json": {"candidates": [CANDIDATE]}})
    c = client(tmp_storage, fake_embed, jev, llm)
    head = (await c.add(messages=trajectory(), user_id="u")).procedure_ids[0]

    llm.responses["json"] = {"candidates": [OVERRIDE]}
    jev.responses = {"scene": "CODING", "0:target": head, "0:action": "ADD_SUPPLEMENT"}
    r = await c.add(messages=trajectory("WAL mode fixed it"), user_id="u")
    new = r.procedure_ids[0]
    old_node, new_node = c.factory.vector.get(head), c.factory.vector.get(new)
    assert old_node.is_latest and new_node.is_latest
    assert new_node.supersedes == [] and new_node.custom["update_type"] == "SUPPLEMENT"
    assert r.decisions[-1].targets == []
    action = [s for s in jev.calls if "0:action" in s["questions"]][-1]
    assert "ADD_MERGE" not in action["questions"]["0:action"]["criteria"]
    await c.aclose()


async def test_heads_outside_the_version_boundary_are_never_offered(
    tmp_storage, fake_embed
):
    jev = FakeJev({"scene": "CODING"})
    c = client(tmp_storage, fake_embed, jev)
    await c.add(messages=trajectory(), user_id="u", workspace_id="/a", branch="main")
    jev.calls.clear()
    await c.add(messages=trajectory(), user_id="u", workspace_id="/a", branch="dev")
    decided = [q for call in jev.calls for q in call["questions"]]
    assert "0:target" not in decided and "0:action" in decided
    jev.calls.clear()
    await c.add(messages=trajectory(), user_id="u", workspace_id="/a", branch="main")
    assert "0:target" in [q for call in jev.calls for q in call["questions"]]
    await c.aclose()


@pytest.mark.parametrize("action,latest", [("DELETE", False), ("SKIP", True)])
async def test_delete_archives_and_skip_keeps_the_head(
    tmp_storage, fake_embed, action, latest
):
    jev = FakeJev({"scene": "CODING"})
    c = client(tmp_storage, fake_embed, jev)
    head = (await c.add(messages=trajectory(), user_id="u")).procedure_ids[0]
    jev.responses = {"scene": "CODING", "0:target": head, "0:action": action}
    r = await c.add(messages=trajectory(), user_id="u")
    procedure_decisions = [
        d for d in r.decisions if d.proposal_ids[0].startswith("procedure:")
    ]
    assert [(d.action, d.targets) for d in procedure_decisions] == [(action, [head])]
    assert r.procedure_ids == [] and r.scene == "coding"
    assert c.factory.vector.get(head).is_latest is latest
    await c.aclose()


async def test_tool_steps_without_dialogue_store_nothing(tmp_storage, fake_embed):
    """Tool calls alone carry no request and no answer: nothing to remember."""
    jev = FakeJev({"scene": "CODING"})
    llm = FakeLLMClient({"json": {"candidates": [CANDIDATE]}})
    c = client(tmp_storage, fake_embed, jev, llm)
    r = await c.add(messages=trajectory()[2:4], user_id="u")
    assert r.memory_id == "" and r.status == "raw_only"
    assert jev.calls == [] and llm.calls == []
    await c.aclose()


async def test_same_request_id_replays_without_model_calls(tmp_storage, fake_embed):
    jev = FakeJev({"scene": "CODING"})
    llm = FakeLLMClient({"json": {"candidates": [CANDIDATE]}})
    c = client(tmp_storage, fake_embed, jev, llm)
    first = await c.add(messages=trajectory(), user_id="u", request_id="t1")
    calls = (len(llm.calls), len(jev.calls))
    again = await c.add(messages=trajectory(), user_id="u", request_id="t1")
    assert (len(llm.calls), len(jev.calls)) == calls
    assert again.procedure_ids == first.procedure_ids
    assert again.extra_node_ids == first.extra_node_ids
    assert len(c.factory.vector.get_many({"kind": PROCEDURE_KIND})) == 1
    await c.aclose()


async def test_generation_failure_keeps_the_trajectory_and_retry_completes(
    tmp_storage, fake_embed
):
    state = {"fail": True}

    def generate(**kwargs):
        if state["fail"]:
            raise RuntimeError("generator unavailable")
        return {"candidates": [CANDIDATE]}

    jev = FakeJev({"scene": "CODING"})
    c = client(tmp_storage, fake_embed, jev, FakeLLMClient({"json": generate}))
    with pytest.raises(RuntimeError, match="generator unavailable"):
        await c.add(messages=trajectory(), user_id="u", request_id="t1")
    raws = c.factory.vector.get_many({"layer": "L0_RAW"})
    assert len(raws) == 1 and "Fix the SDK transaction" in raws[0].content
    assert c.factory.vector.get_many({"kind": PROCEDURE_KIND}) == []

    state["fail"] = False
    r = await c.add(messages=trajectory(), user_id="u", request_id="t1")
    assert r.memory_id == raws[0].node_id and len(r.procedure_ids) == 1
    await c.aclose()


@pytest.mark.parametrize(
    "data",
    [{"candidates": "x"}, {"candidates": [{"task": "t"}]}, {"items": []}],
)
async def test_malformed_generation_fails_loudly(tmp_storage, fake_embed, data):
    c = client(
        tmp_storage, fake_embed, FakeJev({"scene": "CODING"}), FakeLLMClient({"json": data})
    )
    with pytest.raises(CodingDecisionError):
        await c.add(messages=trajectory(), user_id="u")
    await c.aclose()


async def test_invalid_rejects_only_that_draft(tmp_storage, fake_embed):
    jev = FakeJev({"scene": "CODING"})
    llm = FakeLLMClient({"json": {"candidates": [CANDIDATE]}})
    c = client(tmp_storage, fake_embed, jev, llm)
    head = (await c.add(messages=trajectory(), user_id="u")).procedure_ids[0]
    llm.responses["json"] = {"candidates": [CANDIDATE, OVERRIDE]}
    jev.responses = {
        "scene": "CODING",
        "0:target": head,
        "0:action": "INVALID",
        "1:target": "NONE",
        "1:action": "ADD_SUPPLEMENT",
    }
    r = await c.add(messages=trajectory(), user_id="u")
    procedure = [d for d in r.decisions if d.proposal_ids[0].startswith("procedure:")
                 or d.proposal_ids[0] in r.procedure_ids]
    assert [d.action for d in procedure] == ["INVALID", "ADD"]
    assert procedure[0].targets == [] and len(r.procedure_ids) == 1
    assert c.factory.vector.get(head).is_latest
    await c.aclose()


async def test_need_context_is_kept_as_an_independent_version(tmp_storage, fake_embed):
    jev = FakeJev({"scene": "CODING"})
    c = client(tmp_storage, fake_embed, jev)
    head = (await c.add(messages=trajectory(), user_id="u")).procedure_ids[0]
    jev.responses = {"scene": "CODING", "0:target": head, "0:action": "NEED_CONTEXT"}
    r = await c.add(messages=trajectory(), user_id="u")
    (new,) = r.procedure_ids
    node = c.factory.vector.get(new)
    assert node.supersedes == [] and node.custom["update_type"] == "SUPPLEMENT"
    assert c.factory.vector.get(head).is_latest
    assert r.decisions[-1].reason.startswith("NEED_CONTEXT")
    await c.aclose()


async def test_empty_generation_is_raw_only_without_a_decision_call(
    tmp_storage, fake_embed
):
    jev = FakeJev({"scene": "CODING"})
    c = client(
        tmp_storage, fake_embed, jev, FakeLLMClient({"json": {"candidates": []}})
    )
    r = await c.add(messages=trajectory(), user_id="u")
    assert r.scene == "coding" and r.procedure_ids == []
    assert [d.action for d in r.decisions if d.reason.startswith("RAW_ONLY: no explicit")]
    assert all("0:action" not in call["questions"] for call in jev.calls)
    await c.aclose()


async def test_chat_decisions_never_target_a_procedure(tmp_storage, fake_embed):
    jev = FakeJev({"scene": "CODING"})
    c = client(tmp_storage, fake_embed, jev)
    proc = (await c.add(messages=trajectory(), user_id="u")).procedure_ids[0]
    jev.calls.clear()
    # Chat content identical to the procedure task: the nearest neighbour by
    # embedding would be the procedure if the chat path could see it.
    await c.add(content=CANDIDATE["task"], user_id="u")
    assert proc not in json.dumps([call["state"] for call in jev.calls], default=str)
    await c.aclose()


async def test_concurrent_writes_see_each_others_heads(tmp_storage, fake_embed):
    seen: list[list[str]] = []

    def decide(*, state, questions):
        if any(q.startswith("0:") for q in questions) and "current" in state:
            seen.append([m["memory_id"] for m in state["current"]])
        return {"scene": "CODING", "0:target": "NONE"}

    c = client(tmp_storage, fake_embed, FakeJev(decide))
    first, second = await asyncio.gather(
        c.add(messages=trajectory(), user_id="u"),
        c.add(messages=trajectory("again"), user_id="u"),
    )
    assert first.procedure_ids and second.procedure_ids
    # One write planned against an empty scope, the other saw its committed head.
    assert {len(s) for s in seen} == {0, 1}
    await c.aclose()


async def test_dual_digest_covers_procedures_but_catalog_does_not(
    tmp_storage, fake_embed, monkeypatch
):
    from memhub.system2 import system2_agent
    from memhub.system2.system2_agent import System2Agent

    jev = FakeJev({"scene": "CODING"})
    c = client(tmp_storage, fake_embed, jev, mode="dual")
    r = await c.add(messages=trajectory(), user_id="u")
    assert c.factory.cache.list_pending_s2_scopes()
    clustered: list[str] = []
    seen: list[str] = []
    original_cluster = system2_agent.cluster_facts
    original_catalog = System2Agent._catalog

    def cluster(facts, **kwargs):
        clustered.extend(f.node_id for f in facts)
        return original_cluster(facts, **kwargs)

    async def catalog(self, scope, facts):
        seen.extend(f.node_id for f in facts)
        return await original_catalog(self, scope, facts)

    monkeypatch.setattr(system2_agent, "cluster_facts", cluster)
    monkeypatch.setattr(System2Agent, "_catalog", catalog)
    out = await c.digest()
    assert out.processed == 1
    assert r.procedure_ids[0] in clustered
    assert seen and r.procedure_ids[0] not in seen
    assert r.procedure_ids[0] not in seen
    await c.aclose()


async def test_update_replaces_the_solution_and_delete_archives(
    tmp_storage, fake_embed
):
    c = client(tmp_storage, fake_embed, FakeJev({"scene": "CODING"}))
    head = (await c.add(messages=trajectory(), user_id="u")).procedure_ids[0]
    updated = await c.update(head, "Set PRAGMA journal_mode=WAL before publishing")
    assert updated.success
    new = c.factory.vector.get(updated.memory_id)
    assert new.kind == PROCEDURE_KIND and new.supersedes == [head]
    assert new.custom["procedure"]["task"] == CANDIDATE["task"]
    assert new.custom["procedure"]["solution"].startswith("Set PRAGMA")
    assert new.content == f"{CANDIDATE['task']}\nSet PRAGMA journal_mode=WAL before publishing"
    # The question side stays the index, so the same query still finds it.
    expected = await fake_embed.embed_batch(
        ["\n".join([CANDIDATE["task"], *CANDIDATE["search_keys"]])]
    )
    assert new.embedding == pytest.approx(expected[0], abs=1e-6)
    out = await c.search(
        query=CANDIDATE["task"], user_id="u", procedure_limit=5, procedure_min_score=0.0
    )
    assert [m.memory_id for m in out.memories.procedures] == [updated.memory_id]
    assert out.memories.procedures[0].procedure.solution.startswith("Set PRAGMA")

    assert (await c.delete(updated.memory_id)).success
    archived = c.factory.vector.get(updated.memory_id)
    assert not archived.is_latest and archived.custom["procedure"]
    out = await c.search(
        query=CANDIDATE["task"], user_id="u", procedure_limit=5, procedure_min_score=0.0
    )
    assert out.memories.procedures == []
    await c.aclose()


def test_settle_gives_each_head_one_retiring_claim():
    ops = [
        CodingReconcileOp("ADD", 0, "h", "OVERRIDE", confidence=0.6),
        CodingReconcileOp("ADD", 1, "h", "NEGATE", confidence=0.9),
        CodingReconcileOp("DELETE", 2, "h", confidence=0.5),
        CodingReconcileOp("ADD", 3, "h", "CONFLICT", confidence=0.1),
    ]
    out = _settle(ops)
    assert (out[1].update_type, out[1].target_memory_id) == ("NEGATE", "h")
    assert (out[0].update_type, out[0].target_memory_id) == ("SUPPLEMENT", None)
    assert (out[2].action, out[2].target_memory_id) == ("RAW_ONLY", None)
    assert out[3] == ops[3]  # CONFLICT keeps both, it does not retire


async def test_ambiguous_multiple_target_falls_back_to_independent_supplement():
    from memhub.coding.types import CodingMemory

    heads = [CodingMemory(memory_id=i, user_id="u", task=i) for i in ("a", "b")]
    draft = CodingMemoryDraft(task="t", solution="s")

    def decide(*, state, questions):
        if "0:target" in questions:
            return {"0:target": "MULTIPLE"}
        if "0:action" in questions:
            return {"0:action": "ADD_SUPPLEMENT"}
        return {q: "INCLUDE" for q in questions}

    reconciler = CodingMemoryReconciler(jev=FakeJev(decide))
    ops = await reconciler.reconcile(
        [draft], heads, source_messages=[{"role": "user", "content": "x"}]
    )
    assert len(ops) == 1
    assert (ops[0].action, ops[0].update_type, ops[0].target_memory_id) == (
        "ADD",
        "SUPPLEMENT",
        None,
    )


def test_candidate_limit_is_validated():
    with pytest.raises(ValueError):
        CodingMemoryReconciler(jev=FakeJev(), candidate_limit=254)


def test_dialogue_keeps_requests_and_final_answers_only():
    msgs = trajectory()[1:] + [
        {"role": "user", "content": "and the reader path?"},
        {"role": "assistant", "content": "checking", "tool_calls": [
            {"id": "c2", "type": "function",
             "function": {"name": "grep", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "c2", "content": "noise " * 1000},
        {"role": "assistant", "content": "Readers use WAL snapshots."},
    ]
    assert coding_dialogue(msgs) == [
        {"role": "user", "content": "I always use uv, never pip. Fix the SDK transaction"},
        {"role": "assistant", "content": "Done: the publish path now locks first."},
        {"role": "user", "content": "and the reader path?"},
        {"role": "assistant", "content": "Readers use WAL snapshots."},
    ]


def test_fit_dialogue_keeps_every_message_and_user_text_first():
    msgs = [
        {"role": "user", "content": "short ask"},
        {"role": "assistant", "content": "a" * 50_000},
        {"role": "user", "content": "u" * 3_000},
        {"role": "assistant", "content": "b" * 50_000},
    ]
    out = fit_dialogue(msgs, 10_000)
    assert [m["role"] for m in out] == ["user", "assistant", "user", "assistant"]
    assert sum(len(m["content"]) for m in out) <= 10_000
    # User text fits its share untouched; the answers absorb the cut.
    assert out[0]["content"] == "short ask" and out[2]["content"] == "u" * 3_000
    assert out[1]["content"].startswith("a") and out[1]["content"].endswith("a")


async def test_long_coding_session_is_decided_within_the_jev_budget(tmp_storage, fake_embed):
    """A real agentica session is hundreds of tool steps; none may reach Jev."""
    steps = []
    for i in range(300):
        steps += [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": f"c{i}", "type": "function",
                 "function": {"name": "read_file", "arguments": json.dumps({"path": f"f{i}.py"})}}]},
            {"role": "tool", "tool_call_id": f"c{i}", "content": "代码" * 2000},
        ]
    long = [
        {"role": "user", "content": "修一下事务提交的锁问题"},
        *steps,
        {"role": "assistant", "content": "改成 BEGIN IMMEDIATE 了。" * 2000},
    ]
    jev = FakeJev({"scene": "CODING", "0:action": "ADD_SUPPLEMENT"})
    c = client(tmp_storage, fake_embed, jev)
    r = await c.add(messages=long, user_id="u", request_id="long")
    assert r.scene == "coding" and len(r.procedure_ids) == 1
    for call in jev.calls:
        assert len(json.dumps(call["state"], ensure_ascii=False)) < 60_000
        assert "代码代码" not in json.dumps(call["state"], ensure_ascii=False)
    raw = c.factory.vector.get(r.memory_id)
    assert "代码代码" not in raw.content and "修一下事务提交的锁问题" in raw.content
    proc = c.factory.vector.get(r.procedure_ids[0])
    assert proc.custom["procedure"]["files"][:2] == ["f0.py", "f1.py"]
    await c.aclose()


@respx.mock
async def test_extractor_uses_a_json_object_envelope():
    """JSON-object mode can only return an object, never a bare array."""
    route = respx.post("https://api.test/v1/chat/completions").mock(
        return_value=httpx.Response(
            200,
            json={
                "id": "1",
                "object": "chat.completion",
                "created": 0,
                "model": "m",
                "choices": [
                    {
                        "index": 0,
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": json.dumps({"candidates": [CANDIDATE]}),
                        },
                    }
                ],
            },
        )
    )
    llm = LLMClient(base_url="https://api.test/v1", api_key="k", model="m")
    drafts = await CodingMemoryExtractor(llm=llm).extract(
        coding_dialogue(trajectory()[1:]), files=["memhub/store.py"], branch="b"
    )
    body = json.loads(route.calls.last.request.content)
    assert body["response_format"] == {"type": "json_object"}
    assert [(d.task, d.branch, d.files) for d in drafts] == [
        (CANDIDATE["task"], "b", ["memhub/store.py"])
    ]
    await llm.aclose()


def test_format_memories_labels_procedures_by_kind() -> None:
    from memhub.retrieval.formatter import format_memories

    text = format_memories(
        {
            "normal": [{"content": "用户在做支付服务", "category": "fact", "owner": "user"}],
            "procedures": [
                {"content": "去重：按 event_id 幂等", "category": "fact", "kind": "procedure"}
            ],
        },
        date_preamble=False,
    )
    assert "- [fact] [user] 用户在做支付服务" in text
    assert "- [procedure] 去重：按 event_id 幂等" in text


async def test_reconcile_bounds_selected_material_above_candidate_limit_and_fits_pages() -> None:
    """When a project accumulates more procedures than ``candidate_limit`` and JEV
    marks more than ``candidate_limit`` as INCLUDE, the reconciler paginates without
    asking blind questions and bounds the kept heads by confidence + recency instead
    of failing with ``coding selected material exceeds candidate budget``."""
    from memhub.coding.types import CodingMemory
    from memhub.providers.jev import JevAnswer

    heads = [
        CodingMemory(
            memory_id=f"h-{idx:02d}",
            user_id="u",
            task=f"Task {idx}: " + ("x" * 180),
            solution="s" * 400,
            workspace_id="/repo",
            branch="main",
        )
        for idx in range(25)
    ]
    draft = CodingMemoryDraft(
        task="How to configure connection pooling",
        solution="Set max_overflow=10",
        workspace_id="/repo",
        branch="main",
    )
    calls: list[tuple[dict, dict]] = []

    def decide(*, state, questions):
        calls.append((state, questions))
        if "0:target" in questions:
            return {"0:target": "h-22"}
        if "0:action" in questions:
            return {"0:action": "ADD_OVERRIDE"}
        # Screening: mark EVERY head as INCLUDE; give h-22 and h-24 highest confidence.
        out = {}
        for qid in questions:
            conf = 0.99 if qid in ("h-22", "h-24") else 0.75
            out[qid] = JevAnswer(
                type="choice",
                choice="INCLUDE",
                confidence=conf,
                probabilities={"INCLUDE": conf, "EXCLUDE": round(1 - conf, 4)},
            )
        return out

    max_chars = 10_000
    reconciler = CodingMemoryReconciler(jev=FakeJev(decide), candidate_limit=6)
    ops = await reconciler.reconcile(
        [draft],
        heads,
        source_messages=[{"role": "user", "content": "pool config " * 200}],
        max_state_chars=max_chars,
    )
    assert len(ops) == 1
    assert (ops[0].action, ops[0].update_type, ops[0].target_memory_id) == (
        "ADD",
        "OVERRIDE",
        "h-22",
    )
    for state, questions in calls:
        present = {m["memory_id"] for m in state["current"]}
        for qid, q in questions.items():
            # Every screening question must correspond to a head actually in state["current"].
            if qid.startswith("h-"):
                assert qid in present
            # State + individual question must stay within max_state_chars.
            state_len = len(json.dumps(state, ensure_ascii=False, default=str))
            q_len = len(json.dumps({qid: q}, ensure_ascii=False))
            assert state_len + q_len <= max_chars
        if "0:target" in questions:
            offered = set(questions["0:target"]["criteria"]) - {"NONE", "MULTIPLE"}
            assert len(offered) <= 6
            assert {"h-22", "h-24"} <= offered

