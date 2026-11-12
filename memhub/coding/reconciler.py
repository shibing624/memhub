"""JEV owns source support, value, action and current target."""

import json
from typing import Any

from memhub.coding.judge import evaluate_decisions
from memhub.coding.preproc import fit_dialogue
from memhub.coding.types import (
    CodingDecisionError,
    CodingMemory,
    CodingMemoryDraft,
    CodingReconcileOp,
    version_boundary,
)
from memhub.providers.jev import DecisionProvider

_ACTIONS = {
    "ADD_SUPPLEMENT": "Independent learning or a distinct occurrence; append. Initial solution without a baseline belongs here.",
    "ADD_OVERRIDE": "Confirmed replacement of the same task and boundary. Append a complete version. Returning to an ancestor solution overrides the current head, never SKIP.",
    "ADD_TEMPORAL": "Explicit limited exception that coexists with the long-term solution. Do not retire the baseline.",
    "ADD_NEGATE": "Explicit termination of an existing solution.",
    "ADD_CONFLICT": "Competing simultaneous solutions with no explicit change or authority. Keep both; do not retire either.",
    "SKIP": "A CURRENT head already fully covers this claim. The incoming source, the draft itself and history NEVER mean known. NONE is not coverage.",
    "RAW_ONLY": "Supported candidate has no durable persistence value; keep source.",
    "DELETE": "Explicit retraction; archive the current head, do not erase it.",
    "INVALID": "Candidate invents facts or incorrectly attributes source evidence.",
    "NEED_CONTEXT": "Insufficient evidence or a lossless version is unavailable.",
}
_WITH_TARGET = (
    "ADD_OVERRIDE",
    "ADD_NEGATE",
    "ADD_CONFLICT",
    "ADD_SUPPLEMENT",
    "SKIP",
    "DELETE",
    "INVALID",
    "NEED_CONTEXT",
)
_WITHOUT_TARGET = (
    "ADD_SUPPLEMENT",
    "ADD_TEMPORAL",
    "RAW_ONLY",
    "INVALID",
    "NEED_CONTEXT",
)
_NEEDS_TARGET = set(_WITH_TARGET) - {"ADD_SUPPLEMENT", "INVALID", "NEED_CONTEXT"}
# Room the questions and the envelope take next to the state.
_QUESTION_OVERHEAD = 4_000
_OPTION_CHARS = 240
# Every draft of a write carries the same session file list (a long session names
# ~100 paths). It is metadata for retrieval, not material for any decision, so the
# judge sees it once per draft only up to this many paths.
_JUDGE_FILES = 8


def _size(state: dict, overhead: int = _QUESTION_OVERHEAD) -> int:
    return len(json.dumps(state, ensure_ascii=False, default=str)) + overhead


def _max_question_chars(questions: dict[str, dict[str, Any]]) -> int:
    if not questions:
        return _QUESTION_OVERHEAD
    largest = max(
        len(json.dumps({k: q}, ensure_ascii=False)) for k, q in questions.items()
    )
    return max(_QUESTION_OVERHEAD, largest + 1_000)


def _action_question(draft_idx: int, *, has_target: bool) -> dict:
    if has_target:
        instructions = (
            f"Draft {draft_idx} already has one selected current head in the same version boundary. "
            "Its environment constraints must describe the same solution context. "
            "Choose only a relation to that head: SKIP if it fully covers the draft, "
            "ADD_OVERRIDE or ADD_NEGATE to replace or end it, "
            "ADD_CONFLICT to keep both, DELETE to archive it. "
            "INVALID if the draft invents facts. NEED_CONTEXT if a lossless version is unavailable. "
            "ADD_SUPPLEMENT when the draft only adds to the head (a complementary "
            "fragment, not a replacement): both stay current, the head is not retired."
        )
        keys = _WITH_TARGET
    else:
        instructions = (
            f"Draft {draft_idx} has no selected current head. "
            "There is nothing to update, cover, negate, conflict with, or delete. "
            "ADD_SUPPLEMENT appends an independent solution. "
            "ADD_TEMPORAL is a limited exception and does not link a baseline. "
            "RAW_ONLY keeps only the source. INVALID if the draft invents facts. "
            "NEED_CONTEXT if the evidence is insufficient."
        )
        keys = _WITHOUT_TARGET
    return {
        "type": "choice",
        "instructions": instructions,
        "criteria": {key: _ACTIONS[key] for key in keys},
    }


class CodingMemoryReconciler:
    def __init__(self, *, jev: DecisionProvider, candidate_limit: int = 64) -> None:
        if not 1 <= candidate_limit <= 253:
            raise ValueError("coding candidate_limit must be in [1,253]")
        self.jev = jev
        self.candidate_limit = candidate_limit

    async def reconcile(
        self,
        drafts: list[CodingMemoryDraft],
        existing: list[CodingMemory],
        *,
        source_messages: list[dict],
        history_versions: dict[str, list[dict[str, Any]]] | None = None,
        max_state_chars: int = 60_000,
    ) -> list[CodingReconcileOp]:
        if not source_messages:
            raise CodingDecisionError("coding reconciliation requires source evidence")
        histories = history_versions or {}
        current = []
        by_id: dict[str, CodingMemory] = {}
        for mem in existing:
            if not mem.is_latest:
                raise CodingDecisionError("coding current target is retired")
            if mem.memory_id in by_id or mem.memory_id in ("NONE", "MULTIPLE"):
                raise CodingDecisionError("invalid coding current target ID")
            # The version store owns chain integrity; the head shows its ancestors so
            # a return to an earlier solution is recognised as an override.
            current.append(
                dict(mem.to_dict(), history_versions=histories.get(mem.memory_id, []))
            )
            by_id[mem.memory_id] = mem

        if not drafts:
            # Same as the chat path: no candidates keeps the source RAW_ONLY without
            # spending a model call on whether the extractor missed something.
            return []
        proposals = [
            dict(d.to_dict(), id=i, files=d.files[:_JUDGE_FILES]) for i, d in enumerate(drafts)
        ]
        if _size({"source": source_messages, "drafts": proposals}) > max_state_chars:
            drafts_chars = _size({"source": [], "drafts": proposals})
            if drafts_chars <= max_state_chars:
                source_messages = fit_dialogue(
                    source_messages, max(max_state_chars - drafts_chars, 0)
                )
        if _size({"source": source_messages, "drafts": proposals}) > max_state_chars:
            raise CodingDecisionError(
                "coding source and drafts exceed the Jev context budget"
            )
        # A head outside every draft's version boundary can never be a target,
        # so it is not material for any question.
        boundaries = {version_boundary(d) for d in drafts}
        current = [m for m in current if version_boundary(by_id[m["memory_id"]]) in boundaries]
        if len(current) > self.candidate_limit:
            selected: list[tuple[float, int, str]] = []
            cursor = 0
            while cursor < len(current):
                page = list(current[cursor : cursor + self.candidate_limit])
                while (
                    len(page) > 1
                    and _size(
                        {"source": source_messages, "drafts": proposals, "current": page}
                    )
                    > max_state_chars
                ):
                    page.pop()
                if (
                    _size(
                        {"source": source_messages, "drafts": proposals, "current": page}
                    )
                    > max_state_chars
                ):
                    slim = dict(page[0], history_versions=[])
                    if (
                        _size(
                            {
                                "source": source_messages,
                                "drafts": proposals,
                                "current": [slim],
                            }
                        )
                        > max_state_chars
                    ):
                        cursor += 1
                        continue
                    page = [slim]
                answers = await evaluate_decisions(
                    self.jev,
                    state={
                        "source": source_messages,
                        "drafts": proposals,
                        "current": page,
                    },
                    questions={
                        m["memory_id"]: {
                            "type": "choice",
                            "instructions": (
                                "Is this current head or its history potentially relevant "
                                "to ANY draft as coverage, update or boundary baseline? "
                                "Use INCLUDE when unsure; compare complete solutions."
                            ),
                            "criteria": {
                                "INCLUDE": "Relevant",
                                "EXCLUDE": "Clearly unrelated",
                            },
                        }
                        for m in page
                    },
                )
                for offset, m in enumerate(page):
                    ans = answers[m["memory_id"]]
                    if ans.choice == "INCLUDE":
                        selected.append(
                            (
                                float(ans.confidence or 0.0),
                                -(cursor + offset),
                                m["memory_id"],
                            )
                        )
                cursor += len(page)
            ranked_ids = [
                mid
                for _, _, mid in sorted(selected, reverse=True)[: self.candidate_limit]
            ]
            by_selected = {m["memory_id"]: m for m in current}
            current = [by_selected[mid] for mid in ranked_ids if mid in by_selected]

        def build_target_questions(
            items: list[dict[str, Any]],
        ) -> tuple[dict[int, dict[str, str]], dict[str, dict[str, Any]]]:
            # A relation can only link heads in the draft's own version boundary
            # (structured metadata), so other boundaries are never offered as targets.
            per_draft = {
                i: {
                    m["memory_id"]: m["task"][:_OPTION_CHARS]
                    for m in items
                    if version_boundary(by_id[m["memory_id"]]) == version_boundary(draft)
                }
                for i, draft in enumerate(drafts)
            }
            qs: dict[str, dict[str, Any]] = {}
            for i in range(len(drafts)):
                if per_draft[i]:
                    qs[f"{i}:target"] = {
                        "type": "choice",
                        "instructions": (
                            f"Select the one CURRENT head for a relation to draft {i}, "
                            "or NONE when the draft is an independent solution, a limited "
                            "exception, has no durable value, or has independent "
                            "environment constraints. Never select an ancestor or another "
                            "incoming draft. "
                            "Choose MULTIPLE only when the candidates need per-head "
                            "adjudication; expansion must resolve to exactly one head."
                        ),
                        "criteria": {
                            **per_draft[i],
                            "NONE": "No current head",
                            "MULTIPLE": "Needs per-head adjudication",
                        },
                    }
                else:
                    qs[f"{i}:action"] = _action_question(i, has_target=False)
            return per_draft, qs

        # Heads are newest first; the oldest are dropped until state + largest question fits.
        current = list(current)
        heads, questions = build_target_questions(current)
        state = {"source": source_messages, "drafts": proposals, "current": current}
        while current and _size(state, _max_question_chars(questions)) > max_state_chars:
            current.pop()
            heads, questions = build_target_questions(current)
            state = {"source": source_messages, "drafts": proposals, "current": current}
        answers = await evaluate_decisions(self.jev, state=state, questions=questions)
        chosen: dict[int, str | None] = {}
        for i in range(len(drafts)):
            if not heads[i]:
                chosen[i] = None
                continue
            target = answers[f"{i}:target"].choice
            if target == "MULTIPLE":
                include = {
                    f"{i}:{nid}": {
                        "type": "choice",
                        "instructions": (
                            f"Is current head {nid} required for draft {i}?"
                        ),
                        "criteria": {
                            "INCLUDE": "Required",
                            "EXCLUDE": "Unrelated",
                            "UNSURE": "Insufficient evidence",
                        },
                    }
                    for nid in heads[i]
                }
                picked = await evaluate_decisions(
                    self.jev, state=state, questions=include
                )
                # Without MERGE, one procedure cannot retire multiple heads; an
                # uncertain, zero-match or multi-head expansion keeps existing heads
                # current and decides the draft as an independent version.
                if any(item.choice == "UNSURE" for item in picked.values()):
                    chosen[i] = None
                    continue
                matched = [
                    nid
                    for nid in heads[i]
                    if picked[f"{i}:{nid}"].choice == "INCLUDE"
                ]
                chosen[i] = matched[0] if len(matched) == 1 else None
                continue
            chosen[i] = None if target == "NONE" else target
        action_questions = {
            f"{i}:action": _action_question(i, has_target=chosen[i] is not None)
            for i in range(len(drafts))
            if f"{i}:action" not in answers
        }
        if action_questions:
            answers.update(
                await evaluate_decisions(
                    self.jev,
                    state={**state, "selected_targets": chosen},
                    questions=action_questions,
                )
            )
        ops = []
        for i in range(len(drafts)):
            # evaluate_decisions already restricts the choice to the offered criteria.
            action = answers[f"{i}:action"].choice
            target = chosen[i]
            reason = action
            if action == "NEED_CONTEXT":
                # As in the chat path: an undecided relation is written now as an
                # independent version instead of failing the whole write.
                action, target = "ADD_SUPPLEMENT", None
                reason = "NEED_CONTEXT: kept as an independent version"
            if action == "INVALID":
                # Rejects only this draft; the other drafts and the source stay.
                ops.append(
                    CodingReconcileOp(
                        action="INVALID",
                        draft_idx=i,
                        reason=reason,
                        confidence=float(answers[f"{i}:action"].confidence or 0.0),
                    )
                )
                continue
            ops.append(
                CodingReconcileOp(
                    action="ADD" if action.startswith("ADD_") else action,
                    update_type=(
                        action.removeprefix("ADD_") if action.startswith("ADD_") else ""
                    ),
                    draft_idx=i,
                    target_memory_id=target if action in _NEEDS_TARGET else None,
                    reason=reason,
                    confidence=float(answers[f"{i}:action"].confidence or 0.0),
                )
            )
        return ops
