"""Generate complete procedure candidates; JEV owns all semantic decisions."""

import json
import math
from typing import Any

from memhub.coding.types import (
    BOUNDARY_SCOPES,
    CodingDecisionError,
    CodingMemoryDraft,
)
from memhub.providers.llm import LLMClient

_EXTRACT_PROMPT = """Generate candidate procedure memories from the source JSON.
The source is a coding session between a user and a coding agent: the user's
requests and the answers the agent gave (tool calls are already left out).
A procedure is durable, reusable engineering experience (latent rules and non-obvious
lessons): how a non-trivial bug or failure was diagnosed and solved, its root cause,
verified project commands/configurations, architectural or design decisions with
their rationale, explicit engineering rules/constraints set by the user, environment
gotchas, or an approach that failed and why it must be avoided.
Distinguish durable latent rules from ephemeral session state: skip facts that a
future agent can trivially see by reading the current files, routine mechanical
edits/renames with no non-obvious lesson, transient status narration, or unverified
speculation. Do not self-censor genuine rules or solutions merely because they might
already exist in memory: JEV independently decides knownness, coverage and version
updates against existing heads.
The user's requests, corrections and decisions are the primary evidence; take details
from the assistant's answers only as far as they reflect verified outcomes or rules
presented to the user. Personal lifestyle preferences, identity and non-engineering
plans of the user are stored by another extractor; do not restate them here unless
they constrain engineering work.
Preserve complete commands, paths, numbers, reasons, environment boundaries and
qualifications. Do not invent missing details.
Write every text field in the language of the user's messages; do not translate.

Output a JSON object {"candidates": [...]}, each candidate an object with:
- task: string, a self-contained retrieval question
- search_keys: array of strings, alternative phrasings of the task: the user's own
  wording of the problem (verbatim when the source has it), the exact error message
  or symptom, and a short rephrasing someone would search for later
- solution: string, the complete content
- boundary_envs: string, environment constraints in one sentence ("" if none)
- boundary_scope: one of "strict" | "project" | "user" | "global"
- confidence: number in [0, 1]
Return an empty candidates array when the source contains only ephemeral edits or no durable engineering knowledge.
"""


class CodingMemoryExtractor:
    def __init__(self, *, llm: LLMClient) -> None:
        self.llm = llm

    async def extract(
        self,
        messages: list[dict],
        *,
        files: list[str],
        workspace_id: str | None = None,
        branch: str | None = None,
        session_id: str = "",
    ) -> list[CodingMemoryDraft]:
        # An object envelope, not a bare array: JSON mode (llm_json_mode, on by
        # default) only admits objects, and the parser drops anything else.
        data = await self.llm.chat_json(
            system=_EXTRACT_PROMPT,
            user=json.dumps(messages, ensure_ascii=False),
            temperature=0.1,
        )
        items = data.get("candidates") if isinstance(data, dict) else None
        if not isinstance(items, list):
            raise CodingDecisionError(
                'coding extraction requires a {"candidates": [...]} object'
            )
        drafts: list[CodingMemoryDraft] = []
        for item in items:
            if not isinstance(item, dict):
                raise CodingDecisionError("invalid coding candidate object")
            task = self._text(item, "task", required=True)
            solution = self._text(item, "solution", required=True)
            keys = item.get("search_keys", [])
            if not isinstance(keys, list) or any(
                not isinstance(key, str) or not key.strip() for key in keys
            ):
                raise CodingDecisionError("invalid coding candidate search_keys")
            scope = item.get("boundary_scope", "project")
            confidence = item.get("confidence", 0.7)
            if scope not in BOUNDARY_SCOPES:
                raise CodingDecisionError("invalid coding candidate boundary_scope")
            if (
                isinstance(confidence, bool)
                or not isinstance(confidence, (int, float))
                or not math.isfinite(confidence)
                or not 0 <= confidence <= 1
            ):
                raise CodingDecisionError("invalid coding candidate confidence")
            drafts.append(
                CodingMemoryDraft(
                    task=task,
                    search_keys=list(keys),
                    solution=solution,
                    boundary_envs=self._text(item, "boundary_envs"),
                    boundary_scope=scope,
                    confidence=float(confidence),
                    workspace_id=workspace_id,
                    branch=branch,
                    session_id=session_id,
                    files=list(files),
                )
            )
        return drafts

    @staticmethod
    def _text(item: dict[str, Any], name: str, *, required: bool = False) -> str:
        text = item.get(name, "")
        if not isinstance(text, str) or (required and not text.strip()):
            raise CodingDecisionError(f"invalid coding candidate {name}")
        return text
