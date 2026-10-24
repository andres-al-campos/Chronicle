"""Single wrapper around `claude -p`. Security-hardened:

- `--disallowedTools Bash,Write,Edit,NotebookEdit` means Claude can only
  produce text. Critical because conversation content is attacker-controllable
  (past me, copy-pasted material from anywhere); prompt-injection can't trigger
  filesystem writes if Claude has no file tools.
- `--max-budget-usd` caps runaway cost per invocation.
- The Python wrapper is the only thing that writes files.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


class ClaudeNotFoundError(RuntimeError):
    pass


class ClaudeInvocationError(RuntimeError):
    def __init__(self, msg: str, stderr: str = ""):
        super().__init__(msg)
        self.stderr = stderr


def _is_rate_limit_error(result: subprocess.CompletedProcess) -> bool:
    """Check if a failed claude invocation looks like a rate limit."""
    combined = ((result.stderr or "") + (result.stdout or "")).lower()
    return any(s in combined for s in ("rate", "429", "overloaded", "too many"))


def _is_auth_error(result: subprocess.CompletedProcess) -> bool:
    """Check if a failed claude invocation is an authentication failure."""
    combined = ((result.stderr or "") + (result.stdout or "")).lower()
    return any(
        s in combined
        for s in ("401", "authentication_error", "invalid authentication",
                  "failed to authenticate")
    )


_AUTH_HELP = (
    "claude couldn't authenticate (401). Its login is missing or expired — "
    "summaries can't be generated until it's fixed.\n"
    "Fix: run `claude auth login` (then `claude auth status` to confirm), "
    "and re-run the summarize."
)


@dataclass
class Reply:
    text: str
    # The exact model ID that answered, read from the CLI's JSON output, so an
    # alias like "opus" is recorded as the model it resolved to that day.
    # None when the output carried no usage (an old CLI).
    model: str | None


def _parse_reply(stdout: str) -> Reply:
    """Read `claude -p --output-format json` output. Raises on an error result."""
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError:
        # Not JSON: an old CLI that ignores --output-format. Take it as text.
        return Reply(stdout, None)
    if data.get("is_error") or data.get("subtype") not in (None, "success"):
        raise ClaudeInvocationError(
            f"claude returned an error ({data.get('subtype') or 'is_error'}): "
            f"{(data.get('result') or '').strip()[:500]}\n"
            "Fix: check the message above (budget, login, or model name), "
            "then re-run."
        )
    usage = data.get("modelUsage") or {}
    # The answering model does the most output; a side call (a title, a
    # classifier) would use another model for a few tokens.
    model = max(
        usage,
        key=lambda m: (usage[m] or {}).get("outputTokens", 0),
        default=None,
    )
    return Reply(data.get("result") or "", model)


def run_claude(instruction_path: Path, input_text: str, **kwargs) -> str:
    """ask_claude, for callers that only want the text."""
    return ask_claude(instruction_path, input_text, **kwargs).text


def ask_claude(
    instruction_path: Path,
    input_text: str,
    *,
    max_budget_usd: float | None = None,
    timeout_seconds: int = 600,
    model: str | None = None,
    effort: str | None = None,
    max_retries: int = 3,
    retry_wait: int = 30,
) -> Reply:
    """Run `claude -p` with the instruction file prepended to input_text.

    Returns the model's response and the exact model that wrote it. Raises on
    non-zero exit, an error result, or a missing binary. No file writes happen here — caller writes the output.

    On rate-limit errors, retries up to max_retries times with retry_wait
    seconds between attempts.
    """
    binary = shutil.which("claude")
    if not binary:
        raise ClaudeNotFoundError(
            "`claude` binary not found on $PATH. Install Claude Code "
            "(https://claude.com/claude-code) and ensure `claude --version` works, "
            "then try again."
        )

    prompt = instruction_path.read_text(encoding="utf-8") + "\n\n---\n\n" + input_text

    cmd = [
        binary,
        "-p",
        prompt,
        "--disallowedTools",
        "Bash,Write,Edit,NotebookEdit",
        # Override the user's global defaultMode. If they have plan-mode set
        # globally (common), headless `-p` invocations leak preamble like
        # "Plan exit was denied..." into our entry/summary output. Pipeline
        # calls have all write-tools disabled anyway, so plan mode adds no
        # safety — only contamination.
        "--permission-mode",
        "default",
        # JSON carries the exact model that ran alongside the text.
        "--output-format",
        "json",
    ]
    if max_budget_usd is not None:
        cmd.extend(["--max-budget-usd", str(max_budget_usd)])
    if model:
        cmd.extend(["--model", model])
    if effort:
        cmd.extend(["--effort", effort])
    env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

    import time
    last_error: ClaudeInvocationError | None = None

    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            raise ClaudeInvocationError(
                f"claude timed out after {timeout_seconds}s. Re-run with a smaller "
                f"input, or increase timeout via --timeout."
            ) from e

        if result.returncode == 0:
            return _parse_reply(result.stdout)

        # Auth failures won't self-heal — surface the fix immediately, no retry
        # and no raw API-error dump.
        if _is_auth_error(result):
            raise ClaudeInvocationError(_AUTH_HELP, stderr=result.stderr or "")

        # Build the error for reporting / retry decision.
        err_tail = (result.stderr or "").strip().splitlines()[-20:]
        out_tail = (result.stdout or "").strip().splitlines()[-20:]
        parts = [f"claude exited with code {result.returncode}."]
        if err_tail:
            parts.append("Last stderr:\n" + "\n".join(err_tail))
        if out_tail:
            parts.append("Last stdout:\n" + "\n".join(out_tail))
        last_error = ClaudeInvocationError(
            "\n".join(parts),
            stderr=result.stderr or "",
        )

        if _is_rate_limit_error(result) and attempt < max_retries - 1:
            print(
                f"    rate limited — waiting {retry_wait}s "
                f"(attempt {attempt + 1}/{max_retries})",
                flush=True,
            )
            time.sleep(retry_wait)
            continue

        # Non-rate-limit error, or final attempt — don't retry.
        break

    raise last_error
