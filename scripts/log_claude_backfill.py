#!/usr/bin/env python3
"""Recover AI-log entries from Claude Code's own transcripts.

Why this exists
---------------
`log_hook.py` normally writes `.ai-log/session.jsonl` live, driven by the
`UserPromptSubmit` / `PostToolUse` / `Stop` hooks in `.claude/settings.json`.
Those hooks go through `scripts/_pyrun.sh`, and for a while that launcher picked
Windows' App Execution Alias — a `python3.exe` on PATH that prints an advert for
the Microsoft Store and exits 49. Every hook "ran", every hook wrote nothing,
and because the pre-push hook ends its lines in `|| true`, pushes stayed green
while the log stayed empty.

The launcher is fixed. This script is for the sessions lost before it was, and
for any future silent failure: Claude Code keeps a complete transcript of every
session under `~/.claude/projects/<slug>/<session-id>.jsonl`, so the prompts and
tool calls are still on disk even when the hook never saw them.

Deliberately **not** wired into the pre-push hook. With the live hooks working,
a sweep on every push would write each entry a second time. This is a recovery
tool, run by hand, and it is safe to run twice — every entry it would write is
checked against what `.ai-log/` already holds.

Usage
-----
    python scripts/log_claude_backfill.py [--hours N] [--session ID] [--dry-run]

`--hours` bounds how far back to sweep (default 24, matching the Antigravity
sweep's window). `--dry-run` reports what it would write and writes nothing.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import log_hook  # noqa: E402  — same normalisation the live hook uses

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"

#: A slash command as Claude Code stores it in the transcript. The user typed
#: `/implement 44`; the transcript keeps the pieces separately, followed by a
#: second entry holding the skill's body — which is not something the user wrote
#: and must not be logged as though it were.
_COMMAND_NAME = re.compile(r"<command-name>([^<]+)</command-name>")
_COMMAND_ARGS = re.compile(r"<command-args>([^<]*)</command-args>")


def transcripts_for(repo_root: Path) -> list[Path]:
    """Every Claude Code session transcript whose work happened in `repo_root`.

    Matched on the `cwd` recorded inside the transcript rather than on the
    directory name. The directory is a slug of the path (`D:\\Agent-Builder-by-Natural-Languages` becomes
    `D--Agent-Builder-by-Natural-Languages`), and reproducing that slugging here would be a second, silently
    diverging copy of a rule Claude Code owns — the same argument
    `log_antigravity.py` makes for mapping a conversation to a repo by the `Cwd`
    values in its tool calls.
    """
    if not CLAUDE_PROJECTS.is_dir():
        return []
    target = str(repo_root).replace("\\", "/").lower()
    found = []
    for path in sorted(CLAUDE_PROJECTS.glob("*/*.jsonl")):
        if _transcript_cwd(path) == target:
            found.append(path)
    return found


def _transcript_cwd(path: Path) -> str | None:
    """The working directory a transcript belongs to, read from its first entry."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                cwd = entry.get("cwd")
                if cwd:
                    return str(cwd).replace("\\", "/").lower()
    except OSError:
        return None
    return None


def payloads(path: Path):
    """Yield `(timestamp, hook-payload)` pairs for one transcript.

    The payloads are shaped exactly as Claude Code hands them to a hook, so
    `log_hook.normalize` can do the rest. Reconstructing them rather than
    building log entries directly is what keeps this script from becoming a
    second definition of the log format.

    Sidechain entries — a subagent's own turns — are skipped. The `Agent` call
    that spawned it is already logged from the main thread, and the subagent's
    internal tool use is not work the student directed.
    """
    session_id = path.stem
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("isSidechain"):
                continue
            stamp = entry.get("timestamp")
            if not stamp:
                continue

            if entry.get("type") == "user":
                prompt = _user_prompt(entry)
                if prompt:
                    yield stamp, {
                        "hook_event_name": "UserPromptSubmit",
                        "session_id": entry.get("sessionId") or session_id,
                        "prompt": prompt,
                    }

            elif entry.get("type") == "assistant":
                message = entry.get("message") or {}
                for block in message.get("content") or []:
                    if block.get("type") != "tool_use":
                        continue
                    yield stamp, {
                        "hook_event_name": "PostToolUse",
                        "session_id": entry.get("sessionId") or session_id,
                        "model": message.get("model", ""),
                        "tool_name": block.get("name", ""),
                        "tool_input": block.get("input") or {},
                    }


def _user_prompt(entry: dict) -> str:
    """What the student actually typed in this turn, or `""` if they did not.

    Three of the four things that arrive as a `user` entry are not a prompt: a
    tool result coming back, the body of a skill that a slash command expanded
    into, and the `[Request interrupted by user]` marker. Logging any of them
    would pad the log with text the student never wrote — the same mistake the
    deprecated `log_antigravity.py "<summary>"` path made, and the reason
    `.agents/rules/ai-log-hook.md` warns against it.
    """
    message = entry.get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        text = content
    else:
        text = next((b.get("text", "") for b in content or [] if b.get("type") == "text"), "")
    text = (text or "").strip()
    if not text:
        return ""

    named = _COMMAND_NAME.search(text)
    if named:
        args = _COMMAND_ARGS.search(text)
        return f"{named.group(1)} {args.group(1)}".strip() if args else named.group(1)

    # Everything that is not a slash command has to be a prompt Claude Code
    # itself marked as typed. Skill bodies arrive as ordinary user entries with
    # no marker, and there is no wording test that reliably tells one from a
    # long prompt — the marker is the only honest signal.
    if entry.get("promptSource") != "typed":
        return ""
    return text


#: How far apart two records of the same event can be stamped and still be one
#: event. The live hook stamps `datetime.now()` when it fires; this script uses
#: the transcript's timestamp for the event that caused it, and the gap between
#: them is the hook's latency — well under a second in practice.
#:
#: Kept tight, because the two ways of being wrong are not equally bad. Too
#: narrow and a duplicate slips through, which is visible in the log and
#: fixable. Too wide and a real event is silently dropped as a duplicate of its
#: neighbour — and a `UserPromptSubmit` carries no `tool_input`, so every prompt
#: in a session shares one signature and has only this window keeping it
#: distinct from the prompt before it.
_SAME_EVENT_SECONDS = 15


class Ledger:
    """What is already logged, matched by *what happened* rather than by when.

    A plain set keyed on the timestamp does not work, and finding that out is
    what this class is for: a tool call logged live carries `now()` while the
    same call recovered from the transcript carries the transcript's time, so an
    exact-timestamp key called them two different events and wrote both.

    Prompts cannot be matched on their text either — `redact.py` paraphrases
    through an LLM, so the same prompt redacted twice is two different strings.
    What is stable is the tool input (byte-identical from either source) and the
    session, so those carry the identity and the timestamp only has to be close.
    """

    def __init__(self) -> None:
        self._times: dict[tuple, list[datetime]] = {}

    def load(self, log_dir: Path) -> None:
        """Absorb every entry already on disk.

        The archive counts as well as the live file: yesterday's entries are
        rotated out of `session.jsonl`, and a sweep that only read the live file
        would re-add all of them.
        """
        for path in [log_dir / "session.jsonl", *sorted((log_dir / "archive").glob("*.jsonl"))]:
            if not path.exists():
                continue
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    try:
                        self.add(json.loads(line))
                    except (json.JSONDecodeError, TypeError, AttributeError):
                        continue

    def add(self, entry: dict) -> None:
        when = _parse(entry.get("ts", ""))
        if when is not None:
            self._times.setdefault(_signature(entry), []).append(when)

    def holds(self, entry: dict) -> bool:
        """Whether this event is already recorded, allowing for clock skew."""
        when = _parse(entry.get("ts", ""))
        if when is None:
            return False
        return any(
            abs((when - seen).total_seconds()) <= _SAME_EVENT_SECONDS
            for seen in self._times.get(_signature(entry), [])
        )


def _signature(entry: dict) -> tuple:
    """What makes two log entries the same event, timestamp aside.

    Prompt text is deliberately **not** in here. This signature is matched
    against entries already on disk, whose prompts `redact.py` has paraphrased
    through an LLM — the same prompt redacted twice is two different strings, so
    including it would make every comparison fail and duplicate the lot.

    The cost is that all of a session's prompts collapse into one signature, so
    only the timestamp separates them. That is safe here because this is asked
    solely about entries already written — never about two entries of the same
    sweep, where `/implement` and `/implement 44` ten seconds apart are distinct
    prompts that this signature could not tell apart.
    """
    return (
        entry.get("event", ""),
        entry.get("tool_name", ""),
        entry.get("session_id", ""),
        json.dumps(entry.get("tool_input"), sort_keys=True, ensure_ascii=False),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--hours", type=int, default=24, help="how far back to sweep (0 = no limit)")
    parser.add_argument("--session", default="", help="only this session id")
    parser.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = parser.parse_args()

    cutoff = None
    if args.hours > 0:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=args.hours)

    log_dir = log_hook.resolve_log_dir()
    log_dir.mkdir(parents=True, exist_ok=True)
    ledger = Ledger()
    ledger.load(log_dir)

    skipped = 0
    fresh: list[dict] = []
    for path in transcripts_for(REPO_ROOT):
        if args.session and path.stem != args.session:
            continue
        for stamp, payload in payloads(path):
            when = _parse(stamp)
            if when is None or (cutoff is not None and when < cutoff):
                continue

            entry = log_hook.normalize(payload, "claude")
            if entry is None:
                continue
            # The transcript's own time, not now. A backfill that stamped every
            # entry with the moment it ran would flatten a day's work into one
            # instant and lose the only evidence of how it was paced.
            entry["ts"] = when.astimezone(log_hook.VN_TZ).isoformat()

            # Checked against what was on disk *before* this sweep, and nothing
            # collected during it. Every transcript event is yielded exactly
            # once, so there is no such thing as an in-sweep duplicate — and
            # folding fresh entries into the ledger is how two genuinely
            # distinct prompts ten seconds apart get merged into one.
            if ledger.holds(entry):
                skipped += 1
                continue
            fresh.append(entry)

    # Chronological across sessions, not grouped by file. The live hook appends
    # as things happen, so two sessions running over the same afternoon
    # interleave — writing one whole file then the next would produce a log that
    # says the work happened in an order it did not.
    fresh.sort(key=lambda entry: entry["ts"])

    for entry in fresh:
        redacted = _redacted(entry)
        if args.dry_run:
            preview = (redacted.get("prompt") or redacted.get("tool_name") or "")[:70]
            print(f"  + {redacted['ts'][:19]}  {redacted['event']:<16} {preview}")
        else:
            with open(log_dir / "session.jsonl", "a", encoding="utf-8") as handle:
                handle.write(json.dumps(redacted, ensure_ascii=False) + "\n")

    verb = "would add" if args.dry_run else "added"
    print(f"{verb} {len(fresh)} entries, skipped {skipped} already logged")
    return 0


def _redacted(entry: dict) -> dict:
    """Redact prose the student wrote. Leave everything that is not prose alone.

    Two exemptions, both because `redact.py` paraphrases through an LLM and an
    LLM given something that is not prose does not decline — it invents.

    **Tool calls.** `log_hook.normalize` fills `prompt` for a `PostToolUse` event
    from the tool's own `prompt`/`content` input, which for a `Write` is the
    entire file. Sending fifteen source files to OpenAI to be "paraphrased into
    formal Vietnamese" is slow, billed, and replaces the record of what was
    written with prose about it. Redaction exists to generalise the student's
    wording, and a tool call has none.

    **Commands.** A prompt that is a single line starting with `/` or `-` is a
    slash command or a CLI flag. Asked to paraphrase `/model`, the model
    answered it instead — the log came back reading "Bạn đã được đào tạo trên dữ
    liệu đến tháng 10 năm 2023", and `/implement 44` came back as
    `/triển_khai 44`, a command that does not exist. There is no informal
    wording in a command name to clean up, so there is nothing to trade for
    that.
    """
    if entry.get("event") != "UserPromptSubmit":
        entry["prompt"] = ""
        return entry

    prompt = (entry.get("prompt") or "").strip()
    if "\n" not in prompt and prompt[:1] in ("/", "-"):
        return entry

    try:
        from redact import redact_entry
    except ImportError:
        return entry
    return redact_entry(entry)


def _parse(stamp: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(stamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


if __name__ == "__main__":
    os.chdir(REPO_ROOT)  # `log_hook.git()` shells out relative to the cwd.
    sys.exit(main())
