"""A tiny "code review bot" built on the real Claude Agent SDK, the
redundo demo app for the claude-code adapter (the SDK shares it with
the CLI). Drives two real sessions against fixture_repo (a copy handed
to it via --repo, never the checked-in template) and prints each turn's
reply as it streams, so the terminal narrates the story live.

Session A investigates a real test failure and deliberately re-checks
it once more before stopping, without fixing anything: a repeated,
identical, failed call with nothing written in between.
Session B investigates, researches the correct fix online (twice,
rephrased), fixes it, and re-verifies: a real write-then-recheck
loop, plus two similar-not-identical research calls.

A pinned venv with pytest is created inside the working copy by run.sh
before this script runs, and its bin/ directory is put first on PATH
here, so `python3 -m pytest -q` just works every time, deterministically,
without the model needing to go discover an interpreter first. That
discovery detour is realistic (it really does happen against a bare
machine) but it makes each Bash call's arguments a little different
every time, which is death for the exact-repeat story this demo depends
on. So the venv is created ahead of time, off-screen, on purpose.

The system prompt also carries one small appended rule: when asked to
repeat a prior Bash call verbatim, reuse its exact `command` and
`description` arguments unchanged. Redundo's exact-match detection
compares the full tool call, not just the shell command string, so
without this the model's own free variation in the description text
(same command, slightly different wording) would land the repeat in
near_duplicate instead of the clean exact-match story this session is
built to demonstrate.

The test command itself always ends in `|| true`. Confirmed empirically
against a real capture: when a Bash call's exit code is non-zero, Claude
Code's own telemetry never attaches a tool.output content event to that
span at all (regardless of OTEL_LOG_TOOL_CONTENT), so redundo has no
observable result for it and the pair falls into unclassified rather than
confirmed_waste/likely_legitimate. Forcing exit 0 either way keeps the
real pytest output identical either way, just with a shell exit code that
doesn't swallow the telemetry.

Run via run.sh, which points OTEL_EXPORTER_OTLP_ENDPOINT at a real
`redundo collect` receiver first.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
)

_TEST_CMD = "python3 -m pytest -q || true"

SESSION_A_PROMPTS = [
    f"Run the test suite in this repo with `{_TEST_CMD}` and tell me "
    "exactly what it reports. Just that one command for now, don't read "
    "any other files or fix anything yet.",
    "Before we dig deeper, run the exact same test command again, "
    f"verbatim (`{_TEST_CMD}`), to confirm the failure is consistent. "
    "Still nothing else, just that one command.",
]

SESSION_B_PROMPTS = [
    f"Run the test suite with `{_TEST_CMD}` to see what's failing. Just "
    "diagnose it for now, don't fix it yet, we'll want to double check "
    "the right formula first.",
    "Search the web for how a percentage change should be calculated "
    "relative to the original value, to double check the correct formula.",
    "Search the web again, this time specifically for \"percent change "
    "formula old versus new value\", just to be extra sure before making "
    "any changes.",
    "Now fix the bug in calculator.py based on what you found, then "
    "re-run the exact same test command as before, verbatim "
    f"(`{_TEST_CMD}`), to confirm everything passes.",
]

_SYSTEM_PROMPT_APPENDIX = (
    "When a user message explicitly asks you to repeat a previous Bash "
    "call \"verbatim\" or \"the exact same command\", call the Bash tool "
    "again with the identical `command` and `description` arguments as "
    "your immediately preceding Bash call, character for character. Don't "
    "reword the description or adjust the command for that specific call."
)


def _otel_env(repo: str) -> dict[str, str]:
    endpoint = os.environ.get("REDUNDO_OTLP_ENDPOINT", "http://localhost:4318")
    venv_bin = os.path.join(repo, ".venv", "bin")
    return {
        "PATH": venv_bin + os.pathsep + os.environ.get("PATH", ""),
        "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
        "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA": "1",
        "OTEL_TRACES_EXPORTER": "otlp",
        "OTEL_LOGS_EXPORTER": "otlp",
        "OTEL_METRICS_EXPORTER": "none",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "http/protobuf",
        "OTEL_EXPORTER_OTLP_ENDPOINT": endpoint,
        "OTEL_TRACES_EXPORT_INTERVAL": "1000",
        "OTEL_LOGS_EXPORT_INTERVAL": "1000",
        "OTEL_LOG_USER_PROMPTS": "1",
        "OTEL_LOG_TOOL_DETAILS": "1",
        "OTEL_LOG_TOOL_CONTENT": "1",
    }


async def _run_session(label: str, repo: str, prompts: list[str]) -> None:
    print(f"\n\033[1;36m{'=' * 72}\033[0m")
    print(f"\033[1;36m  {label}\033[0m")
    print(f"\033[1;36m{'=' * 72}\033[0m")

    options = ClaudeAgentOptions(
        cwd=repo,
        env=_otel_env(repo),
        permission_mode="bypassPermissions",
        allowed_tools=["Bash", "WebSearch", "Read", "Edit"],
        system_prompt={
            "type": "preset",
            "preset": "claude_code",
            "append": _SYSTEM_PROMPT_APPENDIX,
        },
    )
    async with ClaudeSDKClient(options=options) as client:
        for i, prompt in enumerate(prompts, 1):
            print(f"\n\033[1;33m>> turn {i}:\033[0m {prompt}")
            await client.query(prompt)
            async for message in client.receive_response():
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if isinstance(block, ToolUseBlock):
                            arg_preview = str(block.input)[:100]
                            print(f"   \033[2m[tool] {block.name}({arg_preview})\033[0m")
                        elif isinstance(block, ToolResultBlock):
                            pass  # printed by the tool's own output already
                        elif isinstance(block, TextBlock) and block.text.strip():
                            print(f"   {block.text.strip()}")
    print(f"\n\033[1;32m[{label}: done]\033[0m")


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", required=True, help="Path to the working copy of fixture_repo")
    args = parser.parse_args()

    await _run_session(
        "Session A: investigate (deliberately left unresolved)", args.repo, SESSION_A_PROMPTS,
    )
    await _run_session(
        "Session B: investigate, research, fix, verify", args.repo, SESSION_B_PROMPTS,
    )


if __name__ == "__main__":
    asyncio.run(main())
