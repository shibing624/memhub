# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Generate faithful explicit candidates; Jev decides persistence and relations.
"""
import logging

from memhub.agent import prompts
from memhub.providers.llm import LLMClient, merge_extract_results, truncate_middle
from memhub.sdk_models import ChatMessage

logger = logging.getLogger("memhub.agent.extract")

class Extractor:
    """Extracts identity/fact/intention memories from one LLM JSON call."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        max_content_chars: int = 0,
        few_shot_enabled: bool = False,
    ):
        self.llm = llm
        self.max_content_chars = max_content_chars
        self.few_shot_enabled = few_shot_enabled

    async def extract(
        self,
        *,
        content: str,
        current_time: str,
        app_id: str,
        user_id: str,
        agent_id: str,
        session_id: str,
        current_messages: list[ChatMessage] | None = None,
        history_messages: list[ChatMessage] | None = None,
    ) -> dict:
        """Return extracted fields; ``basic_info`` is persisted later by MemAgent."""
        tmpl = prompts.pick(prompts.EXTRACT_ZH, prompts.EXTRACT_EN, content)
        if self.few_shot_enabled:
            tmpl += prompts.pick(
                prompts.EXTRACT_FEW_SHOT_ZH,
                prompts.EXTRACT_FEW_SHOT_EN,
                content,
            )

        def _build_system(chunk: str) -> str:
            return tmpl.format(
                content=chunk,
                current_time=current_time,
            )

        llm_content = self._prepare_content(
            _message_native_content(current_messages, history_messages)
            if current_messages is not None
            else content
        )
        parsed = await self.llm.chat_json_for_content(
            content=llm_content,
            build_system=_build_system,
            merge_results=merge_extract_results,
        )
        if not isinstance(parsed, dict) or not parsed:
            logger.warning(
                "extract: empty/unparseable LLM output for content len=%d (preview=%r)",
                len(content),
                content[:120],
            )
            parsed = {} if not isinstance(parsed, dict) else parsed

        identity = parsed.get("identity") or []
        facts = parsed.get("facts") or []
        intentions = parsed.get("intentions") or []
        is_ephemeral = bool(parsed.get("is_ephemeral", False))
        basic_info = parsed.get("basic_info")
        if not isinstance(basic_info, dict):
            basic_info = {}

        logger.debug(
            "extract identity=%d facts=%d intentions=%d ephemeral=%s basic_info=%s",
            len(identity) if isinstance(identity, list) else 0,
            len(facts) if isinstance(facts, list) else 0,
            len(intentions) if isinstance(intentions, list) else 0,
            is_ephemeral,
            bool(basic_info),
        )

        out: dict = {
            "identity": identity if isinstance(identity, list) else [],
            "facts": facts if isinstance(facts, list) else [],
            "intentions": intentions if isinstance(intentions, list) else [],
            "is_ephemeral": is_ephemeral,
            "basic_info": basic_info,
        }
        return out

    async def extract_system1(
        self,
        *,
        content: str,
        current_time: str,
        current_messages: list[ChatMessage] | None = None,
        feedback: str = "",
    ) -> dict:
        """Extract System1 memories with a stable contract and separate user input.

        ``feedback`` is the decision provider's rejection of a previous extraction of
        the same source; it is appended verbatim so the regeneration can address it.
        """
        llm_content = self._prepare_content(
            _message_native_content(current_messages, None)
            if current_messages is not None
            else content
        )
        system = (
            "Generate faithful explicit atomic candidates from the supplied conversation. "
            "Use the conversation language. Extract durable user- and task-specific records, not every sentence; "
            "the full conversation is retained separately as source evidence. Jev decides knownness and update relations. "
            "Preserve every explicit personal/project fact, number, name, condition, negation, reason for a choice/change, and explicit plan. "
            "Text the user brings for a task (a draft email, a passage to polish, a message to translate, a post) is "
            "first-person evidence when it is written by or about the user: extract the user's own activities, habits, "
            "possessions, health conditions, relationships, places and experiences it states, as facts about the user "
            "with the task context (e.g. 'In a passage the user wrote about their mornings, the user swims laps at the "
            "university pool before training'). Text written by someone else (an email the user received, a quoted "
            "colleague) is evidence about that person; attribute it to them, never to the user. "
            "Separate distinct events from current state and keep one dimension per candidate. "
            "Make every content self-contained: name its subject and objects and resolve pronouns from this conversation. "
            "Keep a fact's reasons and conditions with that fact, not as disconnected fragments. "
            "Preserve the user's explicit positive/negative evaluation of an experience and the stated feature causing it, "
            "with its context. Do not reduce 'I found X engaging because Y' to only 'the user did X'; "
            "also do not generalize that evaluation to unrelated situations or an enduring trait. "
            "An information request is not a durable goal or preference: do not turn 'explain X' into 'the user wants to learn X'. "
            "But a specific, personal-sounding question is itself an event worth one fact: record the question's "
            "concrete subject as what the user asked about (e.g. 'The user asked how cellulose fiber orientation affects "
            "the rigidity of laminated paper structures'), keeping its specific details, without inferring an interest. "
            "Skip only generic or trivial questions. "
            "Keep concrete assistant-provided details tied to the user's task (names, addresses, phone numbers, amounts, "
            "commands, technical rationale, recommendations), but omit generic explanations, pleasantries, repetitive "
            "procedural advice, and transient operational action or progress narration (such as 'the assistant added/edited/"
            "updated/ran/confirmed...'). "
            "Record what the user states or does; do not generalize it into a broader preference or trait. "
            "When the user asks the assistant to forget, stop using or not remember something about them, that is a "
            "withdrawal, not a new fact: do NOT extract the withdrawn detail (from this request or from earlier "
            "messages of the same conversation) as a user fact, preference or profile item, and add ONE fact "
            "stating only that the user withdrew it and the assistant must not use it, e.g. 'The user withdrew "
            "consent for the assistant to remember or use that the user plays chess; do not personalize with it.' "
            "Never turn assistant advice into a user possession or preference. "
            "When a plan uses an already-owned item ('my device'), retain that possession separately; ownership is not a future plan. "
            "Attribute each candidate to its speaker and explicitly name the assistant in assistant-provided claims. "
            "Repeated statements and return to a former state still require candidates. "
            "Current time is only a reference for explicitly stated relative dates; do not insert an as-of "
            "time or validity boundary when the source supplies none. Do not omit content because it might be known. "
            "For an explicitly limited EXCEPTION or validity window, add valid_from/valid_until "
            "(YYYY-MM-DD) and temporal_scope (verbatim time phrase). Omit all three keys otherwise; never fill defaults. "
            "A reason/condition is NOT a temporal_scope. A plan's scheduled event date is NOT its validity window; "
            "keep the event date in content, without valid_from/valid_until. 'Already own' and 'currently' do not supply validity bounds. "
            'Return {"identity":[{"content":"current user state", "speaker":"user"}], '
            '"facts":[{"content":"event or concrete detail", "speaker":"user|assistant"}], '
            '"intentions":[{"content":"explicit user plan", "speaker":"user"}], "basic_info":{}}. '
            "identity is the profile list: current state, identity, relationship, possession and explicit preference. "
            "Each identity item contains ONE attribute only. Split occupation, residence, diet, relationships, possessions "
            "and preferences into separate objects; never combine them into a profile paragraph. "
            "facts holds events, numbers, project facts and concrete assistant-provided information. "
            "intentions holds explicit future plans/goals, never transient questions. A future plan goes ONLY in intentions, "
            "not also in facts. A completed booking is a fact, distinct from the scheduled future activity. Put each candidate in ONE list; "
            "do not repeat it elsewhere. Empty lists are valid. Always keep basic_info empty. "
            "Do not write placeholder text or explanations. "
            "Empty facts is correct when there are no durable user/task records or concrete factual details."
        )
        if feedback:
            system += f"\nA previous extraction of this source was rejected: {feedback}"
        user = f"Current time: {current_time or '(unknown)'}\n\nConversation:\n{llm_content}"
        parsed = await self.llm.chat_json_for_content(
            content=llm_content,
            build_system=lambda chunk: system
            + f"\nCurrent time: {current_time or '(unknown)'}",
            merge_results=merge_extract_results,
            user=user,
            temperature=0.0,
            max_tokens=3072,
        )
        if not isinstance(parsed, dict):
            parsed = {}
        return {
            "identity": (
                parsed.get("identity")
                if isinstance(parsed.get("identity"), list)
                else []
            ),
            "facts": (
                parsed.get("facts") if isinstance(parsed.get("facts"), list) else []
            ),
            "intentions": (
                parsed.get("intentions")
                if isinstance(parsed.get("intentions"), list)
                else []
            ),
            "is_ephemeral": bool(parsed.get("is_ephemeral", False)),
            "basic_info": (
                parsed.get("basic_info")
                if isinstance(parsed.get("basic_info"), dict)
                else {}
            ),
        }

    def _prepare_content(self, content: str) -> str:
        if self.max_content_chars <= 0 or len(content) <= self.max_content_chars:
            return content
        logger.info(
            "extract: truncating content %d -> %d chars",
            len(content),
            self.max_content_chars,
        )
        return truncate_middle(content, self.max_content_chars)

def _message_native_content(
    current_messages: list[ChatMessage],
    history_messages: list[ChatMessage] | None,
) -> str:
    def _format(messages: list[ChatMessage]) -> str:
        return "\n".join(
            f"[{message.role}]: {message.content} (message_id={index})"
            for index, message in enumerate(messages)
        )

    history = _format(history_messages or []) or "(none)"
    return (
        "Recent context (for reference resolution only; never extract new facts from it):\n"
        f"{history}\n\n"
        "Current messages (extract durable memories only from these messages):\n"
        f"{_format(current_messages)}"
    )
