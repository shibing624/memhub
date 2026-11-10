# -*- coding: utf-8 -*-
"""
@author:XuMing(xuming624@qq.com)
@description: Privacy tag stripping (<private>), multimodal base64 image payload
elision, and automatic secret redaction before evidence enters the Hub store or
replicates to a sync target.
"""
from __future__ import annotations

import json
import re
from typing import Any

_PRIVATE_TAG_RE = re.compile(
    r"<private\b[^>]*>.*?</private\s*>",
    re.DOTALL | re.IGNORECASE,
)

# Inline data:image/*;base64,... payloads above this length are screenshots or
# embedded images rather than tiny icons; replace the base64 body with a byte/char
# marker so transcripts and sync logs never carry megabytes of base64 text.
#
# The body deliberately excludes whitespace: base64 never contains any, and letting
# ``\s`` into the class made the match run past the payload's newline and swallow the
# English prose that followed it (a whole paragraph of real content disappeared).
# It also excludes `)`/`"`/`'`/`<`/`>`, so a payload inside markdown or a JSON string
# stops at its own delimiter.
_DATA_IMAGE_URL_RE = re.compile(
    r"(data:image/[a-zA-Z0-9.+-]+(?:;[^,\s\"'<>)]*)?;base64,)([A-Za-z0-9+/=]{256,})",
    re.IGNORECASE,
)

_MAX_SANITIZE_DEPTH = 12

# High-confidence credential and secret patterns (ported and adapted from claude-mem).
# Order matters: url_credentials runs first so a password inside a URL is replaced
# whole before any vendor token pattern could split it.
# Patterns that need to preserve a variable-width prefix put that prefix in group 1.
_ESCAPED_WS_OR_BOUNDARY = r"(?:(?<=\\[nrt])|\b)"


def _compile_secret(pattern: str, flags: int = 0) -> re.Pattern[str]:
    adapted = re.sub(
        r"(^|\||\(\?:)\\b",
        lambda m: f"{m.group(1)}{_ESCAPED_WS_OR_BOUNDARY}",
        pattern,
    )
    return re.compile(adapted, flags)


BUILTIN_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "url_credentials",
        re.compile(
            r"(\b[a-z][a-z0-9+.-]*://[^\s:/@'\"<>]*:)(?![${<])[^\s/@'\"<>]+(?=@)",
            re.IGNORECASE,
        ),
    ),
    ("aws_access_key", _compile_secret(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    (
        "aws_secret_key",
        re.compile(
            r"((?:aws_secret_access_key|\bSecretAccessKey)(?:\\{0,3}[\"'])?\s{0,8}[=:]\s{0,8}(?:\\{0,3}[\"'])?)[A-Za-z0-9/+=]{40}(?![A-Za-z0-9/+=])",
            re.IGNORECASE,
        ),
    ),
    (
        "github_pat",
        _compile_secret(r"\bgh[oprsu]_[A-Za-z0-9]{36}\b|\bgithub_pat_[A-Za-z0-9_]{82}\b"),
    ),
    ("anthropic_key", _compile_secret(r"\bsk-ant-[A-Za-z0-9_-]{20,}(?!\w)")),
    ("openai_key", _compile_secret(r"\bsk-(?!ant-)[A-Za-z0-9_-]{20,}(?!\w)")),
    ("slack_token", _compile_secret(r"\b(?:xox[abeprs]|xapp)-[A-Za-z0-9-]{10,}(?!\w)")),
    (
        "jwt",
        _compile_secret(r"\beyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+(?!\w)"),
    ),
    (
        "private_key_pem",
        re.compile(
            r"-----BEGIN (?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"
            # Never past the next BEGIN: with many unclosed headers a plain lazy
            # ``[\s\S]*?`` rescans to the end from each one (quadratic).
            r"(?:(?!-----BEGIN )[\s\S])*?"
            r"-----END (?:RSA |DSA |EC |OPENSSH |PGP |ENCRYPTED )?PRIVATE KEY(?: BLOCK)?-----"
        ),
    ),
    (
        "stripe_key",
        _compile_secret(
            r"\b(?:(?:sk|pk|rk)_(?:live|test)_[A-Za-z0-9]{24,}|whsec_[A-Za-z0-9]{32,})\b"
        ),
    ),
    ("google_api_key", _compile_secret(r"\bAIza[0-9A-Za-z_-]{35}(?!\w)")),
    ("google_oauth_token", _compile_secret(r"\bya29\.(?:c\.)?[0-9A-Za-z_-]{20,}(?!\w)")),
    (
        "gitlab_token",
        _compile_secret(r"\bgl(?:pat|dt|rt)-[A-Za-z0-9_-]{20,}(?:\.[A-Za-z0-9_-]+)*(?!\w)"),
    ),
    ("huggingface_token", _compile_secret(r"\bhf_[A-Za-z0-9]{30,}\b")),
    ("npm_token", _compile_secret(r"\bnpm_[A-Za-z0-9]{36}\b")),
    ("docker_pat", _compile_secret(r"\bdckr_pat_[A-Za-z0-9_-]{27,}(?!\w)")),
    ("pypi_token", _compile_secret(r"\bpypi-AgE[A-Za-z0-9_-]{50,}(?!\w)")),
    ("groq_key", _compile_secret(r"\bgsk_[A-Za-z0-9]{52}(?!\w)")),
    ("xai_key", _compile_secret(r"\bxai-[A-Za-z0-9_]{80}(?!\w)")),
    ("perplexity_key", _compile_secret(r"\bpplx-[A-Za-z0-9]{48}(?!\w)")),
    (
        "slack_webhook",
        re.compile(
            r"(hooks\.slack\.com/(?:services|workflows|triggers)/T[A-Z0-9]+/[A-Z0-9]+/(?:[0-9]+/)?)[A-Za-z0-9]{20,}(?!\w)",
            re.IGNORECASE,
        ),
    ),
    (
        "discord_webhook",
        re.compile(
            r"(discord(?:app)?\.com/api/(?:v\d+/)?webhooks/[0-9]{17,20}/)[A-Za-z0-9_-]{60,}(?![\w-])",
            re.IGNORECASE,
        ),
    ),
)


def strip_private_tags(text: str) -> str:
    """Remove ``<private>...</private>`` blocks from text.

    An unclosed ``<private>`` drops everything after it: the user opened a private
    region and the payload must not be stored just because the closing tag was
    missing.
    """
    if not text or "<private" not in text.lower():
        return text
    cleaned = _PRIVATE_TAG_RE.sub("", text)
    opened = re.search(r"<private\b[^>]*>", cleaned, re.IGNORECASE)
    if opened is not None:
        cleaned = cleaned[: opened.start()]
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()


def elide_data_image_urls(text: str) -> str:
    """Replace inline ``data:image/...;base64,...`` payloads with an elision marker."""
    if not text or "base64," not in text.lower():
        return text
    return _DATA_IMAGE_URL_RE.sub(
        lambda m: f"{m.group(1)}<elided {len(m.group(2))} chars>",
        text,
    )


def strip_image_payloads(value: Any, _depth: int = 0) -> Any:
    """Recursively strip base64 image payloads from strings, lists, and content block dicts."""
    if isinstance(value, str):
        elided = elide_data_image_urls(value)
        if elided != value or _depth > _MAX_SANITIZE_DEPTH or len(value) <= 256:
            return elided
        if value[0] in ("{", "[") and ("image" in value.lower() or "base64" in value.lower()):
            try:
                parsed = json.loads(value)
            except json.JSONDecodeError:
                return elided
            stripped = strip_image_payloads(parsed, _depth + 1)
            if stripped != parsed:
                return json.dumps(stripped, ensure_ascii=False)
        return elided

    if _depth > _MAX_SANITIZE_DEPTH or value is None or not isinstance(value, (list, dict)):
        return value

    if isinstance(value, list):
        changed = False
        out_list = []
        for item in value:
            nxt = strip_image_payloads(item, _depth + 1)
            if nxt is not item:
                changed = True
            out_list.append(nxt)
        return out_list if changed else value

    # Dict shapes:
    # 1. Anthropic image block: {"type": "image", "source": {"data": "<base64>", ...}}
    if value.get("type") == "image":
        source = value.get("source")
        if isinstance(source, dict) and isinstance(source.get("data"), str):
            data_str = source["data"]
            elided_src: dict[str, Any] = {
                "elided": "image data withheld",
                "bytes": len(data_str),
            }
            if isinstance(source.get("media_type"), str):
                elided_src["media_type"] = source["media_type"]
            return {"type": "image", "source": elided_src}
        # 2. Claude Read image block: {"type": "image", "file": {"base64": "<base64>", ...}}
        file_obj = value.get("file")
        if isinstance(file_obj, dict) and isinstance(file_obj.get("base64"), str):
            b64_str = file_obj["base64"]
            elided_file: dict[str, Any] = {
                "elided": "image data withheld",
                "bytes": len(b64_str),
            }
            mime = file_obj.get("type") or file_obj.get("media_type")
            if isinstance(mime, str):
                elided_file["media_type"] = mime
            return {"type": "image", "file": elided_file}
        # 3. MCP inline image block: {"type": "image", "data": "<base64>", "mimeType": ...}
        if isinstance(value.get("data"), str):
            elided_mcp: dict[str, Any] = {
                "type": "image",
                "elided": "image data withheld",
                "bytes": len(value["data"]),
            }
            if isinstance(value.get("mimeType"), str):
                elided_mcp["mimeType"] = value["mimeType"]
            return elided_mcp

    # 4. OpenAI image_url block: {"type": "image_url", "image_url": {"url": "data:..."}}
    if value.get("type") == "image_url" and isinstance(value.get("image_url"), dict):
        img_url = value["image_url"]
        url = img_url.get("url")
        if isinstance(url, str) and url[:5].lower() == "data:":
            return {
                "type": "image_url",
                "image_url": {"elided": "image data withheld", "bytes": len(url)},
            }

    changed_dict = False
    out_dict: dict[str, Any] = {}
    for k, v in value.items():
        nxt = strip_image_payloads(v, _depth + 1)
        if nxt is not v:
            changed_dict = True
        out_dict[k] = nxt
    return out_dict if changed_dict else value


def redact_secrets(text: str) -> str:
    """Replace recognized high-confidence secrets with ``<redacted type='...'/>``."""
    if not text:
        return text
    working = text
    for name, regex in BUILTIN_SECRET_PATTERNS:
        marker = f"<redacted type='{name}'/>"
        if regex.groups >= 1:
            working = regex.sub(rf"\g<1>{marker}", working)
        else:
            working = regex.sub(marker, working)
    return working


def sanitize_text(text: str, *, redact: bool = False) -> str:
    """Strip ``<private>`` spans, elide base64 images, and optionally redact secrets.

    ``redact`` defaults to off: this is a personal assistant, and a user who says
    "remember my key is X" means it. Callers that persist third-party tool payloads
    opt in with ``settings.redact_secrets``.
    """
    if not text:
        return text
    cleaned = strip_private_tags(text)
    if not cleaned:
        return ""
    cleaned = str(strip_image_payloads(cleaned))
    return redact_secrets(cleaned) if redact else cleaned


def _sanitize_value(value: Any, *, redact: bool, _depth: int = 0) -> Any:
    """``sanitize_text`` applied to every string inside content blocks / tool args."""
    if isinstance(value, str):
        return sanitize_text(value, redact=redact)
    if _depth > _MAX_SANITIZE_DEPTH:
        return value
    if isinstance(value, list):
        return [_sanitize_value(v, redact=redact, _depth=_depth + 1) for v in value]
    if isinstance(value, dict):
        return {
            k: _sanitize_value(v, redact=redact, _depth=_depth + 1)
            for k, v in value.items()
        }
    return value


def sanitize_messages(
    messages: list[dict] | None, *, redact: bool = False
) -> list[dict] | None:
    """Sanitize a list of chat/tool messages before persistence.

    Only the ``<private>`` spans are removed; the rest of the turn — including the
    assistant's answer — is kept. A message left empty by that removal is dropped,
    so it never reaches extraction as a blank turn.
    """
    if not messages:
        return messages
    sanitized: list[dict] = []
    for msg in messages:
        item = dict(msg)
        content = item.get("content")
        if isinstance(content, str):
            item["content"] = sanitize_text(content, redact=redact)
        elif content is not None:
            item["content"] = _sanitize_value(strip_image_payloads(content), redact=redact)
        if item.get("tool_calls") and isinstance(item["tool_calls"], list):
            calls = []
            for call in item["tool_calls"]:
                c = dict(call)
                fn = c.get("function")
                if isinstance(fn, dict):
                    fn_copy = dict(fn)
                    args = fn_copy.get("arguments")
                    if isinstance(args, str):
                        fn_copy["arguments"] = sanitize_text(args, redact=redact)
                    elif args is not None:
                        fn_copy["arguments"] = _sanitize_value(
                            strip_image_payloads(args), redact=redact
                        )
                    c["function"] = fn_copy
                calls.append(c)
            item["tool_calls"] = calls
        # Drop pure user/assistant text messages whose entire content was inside <private>.
        if not item.get("content") and not item.get("tool_calls"):
            continue
        sanitized.append(item)
    return sanitized


__all__ = [
    "BUILTIN_SECRET_PATTERNS",
    "elide_data_image_urls",
    "redact_secrets",
    "sanitize_messages",
    "sanitize_text",
    "strip_image_payloads",
    "strip_private_tags",
]
