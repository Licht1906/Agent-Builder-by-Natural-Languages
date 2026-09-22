"""Does this spec look like it is carrying a credential?

`AgentSpec` is stored plaintext in Postgres and rendered on the version diff
screen (§7). `connections[].credential_ref` is a `vault://` *pointer* and every
other field is prose or an identifier, so anything here shaped like a credential
arrived by mistake — a user pasting a DSN into a description, or a model
hallucinating one into a field it was told to fill. FR-22 makes rejecting it the
API's job, not a reviewer's.

Two design notes:

**Shape, not truth.** This cannot tell a live key from a placeholder and does not
try. `sk-` followed by forty characters is refused whether or not it opens
anything, because the alternative — deciding which ones are real — is a check that
fails open.

**Never echo the match.** A finding names the rule and the path and stops there.
A detector that quotes the credential into an error message copies it into every
log and response that error touches, which is the leak it was built to prevent.
"""

import math
import re
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

# A minimum length below which "high entropy" says nothing: short identifiers like
# `v_ar_open` or `SALES` are dense by accident, not by construction.
_OPAQUE_TOKEN_MIN_LENGTH = 32
_OPAQUE_TOKEN_MIN_ENTROPY = 3.5

_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    # scheme://user:password@host — the classic paste, and the one §15's demo uses.
    ("dsn_with_credentials", re.compile(r"[a-z][a-z0-9+.-]*://[^\s:/@]+:[^\s/@]+@[^\s/]+", re.IGNORECASE)),
    ("private_key_block", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----")),
    ("openai_api_key", re.compile(r"\bsk-[A-Za-z0-9_-]{20,}")),
    ("aws_access_key_id", re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    ("slack_token", re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}")),
    ("google_api_key", re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b")),
    ("json_web_token", re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
    # `password: something`, `api_key = something` — a labelled secret, whatever its shape.
    (
        "labelled_credential",
        re.compile(
            r"\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|client[_-]?secret)\b\s*[:=]\s*\S{6,}",
            re.IGNORECASE,
        ),
    ),
)


@dataclass(frozen=True)
class Finding:
    """Where a credential-shaped string sits, and which rule saw it. Never the string."""

    rule: str
    path: str


def scan_for_secrets(raw: Any) -> list[Finding]:
    """Walk a spec payload and report every credential-shaped string in it.

    Takes the *raw* payload rather than a parsed `AgentSpec`: this has to run
    before Pydantic, whose errors quote the values that failed them.
    """
    return [finding for path, text in _strings_in(raw, "") for finding in _findings_in(text, path)]


def _findings_in(text: str, path: str) -> Iterator[Finding]:
    for rule, pattern in _RULES:
        if pattern.search(text):
            yield Finding(rule=rule, path=path)
            return
    if _is_opaque_high_entropy_token(text):
        yield Finding(rule="opaque_high_entropy_token", path=path)


def _strings_in(node: Any, path: str) -> Iterator[tuple[str, str]]:
    """Every string in the payload, keys included, paired with where it sits.

    Keys are scanned too: a spec is rejected wholesale if it carries a credential,
    and `{"postgres://user:pw@host": true}` in some extra field is still a leak
    even though the shape check would refuse the field a moment later.
    """
    if isinstance(node, str):
        yield path, node
    elif isinstance(node, Mapping):
        for key, value in node.items():
            child = f"{path}.{key}" if path else str(key)
            if isinstance(key, str):
                yield child, key
            yield from _strings_in(value, child)
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
        for index, item in enumerate(node):
            yield from _strings_in(item, f"{path}[{index}]")


def _is_opaque_high_entropy_token(text: str) -> bool:
    """A long unbroken run of dense characters — the shape of a key with no recognisable prefix.

    Restricted to single tokens with no whitespace, so prose never reaches the
    entropy test. The vocabulary a spec legitimately holds — `vault://conn/erp_readonly`,
    `golden/uc1_ar_v3.jsonl`, column names — is both shorter than the floor and far
    below it in entropy.
    """
    candidate = text.strip()
    if len(candidate) < _OPAQUE_TOKEN_MIN_LENGTH or any(character.isspace() for character in candidate):
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=_-]+", candidate):
        return False
    return _shannon_entropy(candidate) >= _OPAQUE_TOKEN_MIN_ENTROPY


def _shannon_entropy(text: str) -> float:
    counts = {character: text.count(character) for character in set(text)}
    length = len(text)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())
