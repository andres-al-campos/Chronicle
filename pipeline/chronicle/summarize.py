"""`chronicle summarize` — the only command that calls Claude (besides synthesize).

Targets are chosen explicitly by the user:
- --uuid X        : one conversation
- --all-stale     : every conversation where summary_stale() is True (default)
- --period YYYY-MM: every non-deleted conversation whose created_at falls in
                    that month

For each target: read the per-conversation JSON + pending.md (as context),
invoke `claude -p` with files/summarize.txt, capture stdout, write
data/summaries/YYYY-MM/{uuid}.md, update state.summarized_at. Errors print
and continue.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import pending as pending_mod
from . import state as state_mod
from .claude_invoke import (
    ClaudeInvocationError,
    ClaudeNotFoundError,
    run_claude,
)
from .metrics import compression_ratio, measure_text
from .paths import (
    branches_dir,
    data_root,
    ensure_dirs,
    instructions_dir,
    pending_file,
    stem_for,
    summaries_dir,
)
from .state import now_iso


def _instruction_file() -> Path:
    return instructions_dir() / "summarize.txt"


def _load_branches(uuid: str, created_at: str) -> dict[str, Any] | None:
    """Load a branch file if one exists. Returns the parsed branches or None."""
    month = (created_at or "unknown")[:7]
    branch_path = branches_dir() / month / f"{uuid}.json"
    if not branch_path.exists():
        return None
    try:
        return json.loads(branch_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def _delete_branches(uuid: str, created_at: str) -> None:
    month = (created_at or "unknown")[:7]
    branch_path = branches_dir() / month / f"{uuid}.json"
    if branch_path.exists():
        branch_path.unlink()


def _format_branch_messages(messages: list[dict[str, Any]]) -> str:
    """Format a list of raw messages into readable text for the summarizer."""
    import json
    # Wrap in a minimal conversation shell so strip_conversation works
    shell = {"messages": messages}
    return json.dumps(shell, ensure_ascii=False)


def _inject_metrics(
    output: str,
    orig_words: int,
    summary_metrics: dict[str, int],
    ratio: float,
    model: str,
    *,
    identity: dict[str, Any] | None = None,
) -> str:
    """Merge ground-truth identity, metrics, and model into the frontmatter.

    Parse → mutate dict → reserialize, so a `---` thematic break in the
    summary body can never be mistaken for the closing fence. Existing
    keys Claude wrote are preserved in order; ours are appended.

    `identity` (when provided) stamps uuid, title, created_at, last_active,
    and project from the pipeline's own records — never trusting Claude to
    copy these correctly (long conversations cause truncation/hallucination).
    """
    from .metrics import render_with_frontmatter, split_frontmatter

    fields, body = split_frontmatter(output)

    # Overwrite identity fields with ground-truth values from the pipeline.
    conv_model_val = None
    if identity:
        fields["uuid"] = identity["uuid"]
        fields["title"] = identity.get("title") or "untitled"
        if identity.get("created_at"):
            fields["created_at"] = identity["created_at"][:10]  # YYYY-MM-DD
        if identity.get("updated_at"):
            fields["last_active"] = identity["updated_at"]
        if identity.get("project_name"):
            fields["project"] = identity["project_name"]
        conv_model_val = identity.get("conversation_model")

    # Rebuild the dict with controlled key order. The goal:
    #   identity fields → summarized_at → models (adjacent) → Claude's
    #   content fields → metrics at the bottom.
    ts = now_iso()
    ordered: dict[str, Any] = {}
    for k, v in fields.items():
        # Skip keys we'll place explicitly below.
        if k in ("summarized_at", "summary_model", "conversation_model",
                 "model", "original_words", "summary_words", "compression_ratio"):
            continue
        ordered[k] = v
        if k == "last_active":
            ordered["summarized_at"] = fields.get("summarized_at") or ts
            ordered["conversation_model"] = conv_model_val or fields.get("conversation_model") or ""
            ordered["summary_model"] = model
    # Safety: if last_active wasn't in fields (shouldn't happen), append.
    if "summarized_at" not in ordered:
        ordered["summarized_at"] = ts
        ordered["conversation_model"] = conv_model_val or fields.get("conversation_model") or ""
        ordered["summary_model"] = model
    ordered["original_words"] = orig_words
    ordered["summary_words"] = summary_metrics["words"]
    ordered["compression_ratio"] = ratio
    return render_with_frontmatter(ordered, body)


import re as _re

_SUMMARY_SUBHEADER_RE = _re.compile(
    r"^\*\*\d[\d,]+ words → \d[\d,]+ words · [\d.]+ ratio(?:\s+·\s+\S+)?\*\*\n*",
    _re.MULTILINE,
)


def _inject_summary_subheader(
    output: str, orig_words: int, summary_words: int, ratio: float,
    conversation_model: str = "",
) -> str:
    """Insert/replace a stats line as the first body line of a summary.

    Format: **X,XXX words → YYY words · 0.XXXX ratio · model-name**

    Idempotent: strips any existing stats line before inserting.
    """
    from .metrics import render_with_frontmatter, split_frontmatter

    fields, body = split_frontmatter(output)
    # Remove any existing stats line (idempotent).
    body = _SUMMARY_SUBHEADER_RE.sub("", body, count=1)
    # Strip leading blank lines so we don't get double-spacing.
    body = body.lstrip("\n")
    model_suffix = f" · {conversation_model}" if conversation_model else ""
    stats = f"**{orig_words:,} words → {summary_words:,} words · {ratio:.4f} ratio{model_suffix}**"
    new_body = f"{stats}\n\n{body}"
    if fields:
        return render_with_frontmatter(fields, new_body)
    return new_body


def _read_pending_context() -> str:
    p = pending_file()
    if p.exists():
        return p.read_text(encoding="utf-8")
    return "(no pending.md — nothing flagged by recent ingest)"


def _select_targets(state: dict[str, Any], args: Any) -> tuple[list[str], int, str]:
    """Return (stale_uuids, total_in_scope, scope_desc).

    total_in_scope counts non-deleted conversations matching the filter,
    regardless of staleness — lets the caller distinguish "no conversations
    here" from "all caught up". scope_desc is a human label for messages.
    """
    convs = state["conversations"]
    force = getattr(args, "force", False)

    # -dn / --date-now is sugar for `-d <date> now`. Normalize early so the
    # rest of this function only has to handle args.date.
    if getattr(args, "date_now", None):
        args.date = [args.date_now, "now"]

    def _is_target(c: dict) -> bool:
        """True if this conversation should be summarized."""
        if force:
            return not c.get("deleted_at")
        return state_mod.summary_stale(c)

    if args.uuid:
        if args.uuid not in convs:
            raise SystemExit(
                f"UUID {args.uuid} not in state.json. Run `chronicle ingest` first "
                f"or double-check the UUID."
            )
        c = convs[args.uuid]
        in_scope = 0 if c.get("deleted_at") else 1
        targets = [args.uuid] if _is_target(c) else []
        return targets, in_scope, f"UUID {args.uuid[:8]}"
    if getattr(args, "date", None):
        from .calendar import PeriodParseError, parse_period
        labels = args.date if isinstance(args.date, list) else [args.date]
        if len(labels) == 1:
            try:
                if labels[0].lower() == "now":
                    from datetime import date as _date
                    today = _date.today().isoformat()
                    _tier, rs, re_ = "day", today, today
                else:
                    _tier, rs, re_ = parse_period(labels[0])
            except PeriodParseError as e:
                raise SystemExit(str(e))
        elif len(labels) == 2:
            from datetime import date as _date
            today_iso = _date.today().isoformat()

            def _resolve(lbl: str) -> tuple[str, str, str]:
                if lbl.lower() == "now":
                    return ("day", today_iso, today_iso)
                return parse_period(lbl)

            try:
                t1, s1, _e1 = _resolve(labels[0])
                t2, _s2, e2 = _resolve(labels[1])
            except PeriodParseError as e:
                raise SystemExit(str(e))
            if t1 != "day" or t2 != "day":
                raise SystemExit(
                    f"Two-argument --date requires single-day labels (YYYY-MM-DD or 'now'), "
                    f"got '{labels[0]}' ({t1}) and '{labels[1]}' ({t2}). "
                    f"For wider spans use a single label like 2026_Mar_H2."
                )
            if s1 > e2:
                raise SystemExit(
                    f"Range start {labels[0]} is after end {labels[1]}. Swap them."
                )
            rs, re_ = s1, e2
        else:
            raise SystemExit(
                f"--date takes 1 or 2 values, got {len(labels)}: {labels}"
            )
        rows = state_mod.conversations_in_period(state, rs, re_)
        targets = [uuid for uuid, c in rows if _is_target(c)]
        scope = (
            f"{labels[0]} → {labels[1]}" if len(labels) == 2 else labels[0]
        )
        return targets, len(rows), f"date {scope} ({rs} → {re_})"
    # default: all stale across every tracked conversation
    alive = [(u, c) for u, c in convs.items() if not c.get("deleted_at")]
    targets = [u for u, c in alive if _is_target(c)]
    return targets, len(alive), "all tracked conversations"


def summarize_one(
    uuid: str,
    state: dict[str, Any],
    *,
    pending_context: str,
    model: str | None = None,
    effort: str | None = None,
) -> bool:
    """Returns True on success, False on failure. Mutates state on success."""
    conv_meta = state["conversations"].get(uuid)
    if not conv_meta:
        print(f"  ✗ {uuid[:8]} — not in state (skipping)", flush=True)
        return False
    title = conv_meta.get("title") or "(untitled)"
    print(f"  → {uuid[:8]} — \"{title[:70]}\"", flush=True)

    conv_path = data_root() / conv_meta["conversation_file"]
    if not conv_path.exists():
        print(
            f"  ✗ {uuid[:8]} — conversation file missing at {conv_path}. "
            f"Re-ingest the source export.",
            flush=True,
        )
        return False

    from .preprocess import (
        MAX_CHARS,
        chunk_messages,
        estimate_tokens,
        needs_chunking,
        strip_conversation,
    )

    conv_text_raw = conv_path.read_text(encoding="utf-8")
    # Extract the model used in the original conversation (top-level "model"
    # key in the JSON) before stripping — strip_conversation may discard it.
    try:
        conv_model = json.loads(conv_text_raw).get("model")
    except (json.JSONDecodeError, AttributeError):
        conv_model = None
    raw_tokens = estimate_tokens(conv_text_raw)
    conv_text = strip_conversation(conv_text_raw)
    stripped_tokens = estimate_tokens(conv_text)
    significance = conv_meta.get("significance")

    reduction = 1 - stripped_tokens / raw_tokens if raw_tokens else 0
    if reduction > 0.05:
        print(
            f"    stripped tool_use/thinking: ~{raw_tokens:,} → ~{stripped_tokens:,} tokens "
            f"({reduction:.0%} reduction)",
            flush=True,
        )

    orig_chars = conv_meta.get("original_chars") or 0
    orig_words = conv_meta.get("original_words") or 0
    orig_tokens = conv_meta.get("original_tokens_est") or 0
    # Always compute the conditional target so the model sees a concrete
    # number. It's framed as "if you judge this high-significance" so it
    # doesn't force anything — but removes the need for the model to do
    # the 10% math itself (which it won't).
    high_floor = max(int(orig_words * 0.10), 500) if orig_words else 500
    target_line = (
        f"If you judge this high-significance: minimum {high_floor:,} words "
        f"(10% of {orig_words:,}).\n"
    )

    metrics_block = (
        f"# Original conversation metrics (prose only, excludes JSON wrapper)\n\n"
        f"original_chars: {orig_chars}\n"
        f"original_words: {orig_words}\n"
        f"original_tokens_est: {orig_tokens}\n"
        f"{target_line}\n"
        f"After you write your summary, the wrapper computes summary_chars / "
        f"summary_words / summary_tokens_est / compression_ratio and appends "
        f"them to the frontmatter automatically. You do NOT need to include "
        f"them yourself — leave length metrics out of your output.\n"
    )

    # --- Incremental summarization check ---
    # If a branch file exists from ingest (conversation was updated after
    # being summarized), we can pass the old summary + the divergent
    # branches instead of re-processing the entire conversation.
    created_at = conv_meta.get("created_at") or ""
    branches = _load_branches(uuid, created_at)
    old_summary_path = (
        (data_root() / conv_meta["summary_file"])
        if conv_meta.get("summary_file")
        else None
    )
    use_incremental = (
        branches is not None
        and significance in ("medium", "high")
        and old_summary_path is not None
        and old_summary_path.exists()
    )

    if branches and not use_incremental:
        reason = (
            "low significance" if significance == "low"
            else "no existing summary"
        )
        print(f"    branches exist but not eligible for incremental ({reason}) → full re-summarize", flush=True)
        _delete_branches(uuid, created_at)

    if use_incremental:
        old_summary = old_summary_path.read_text(encoding="utf-8")
        is_append = branches.get("is_append_only", False)
        old_branch = branches.get("old_branch_messages", [])
        new_branch = branches.get("new_branch_messages", [])

        new_branch_json = _format_branch_messages(new_branch)
        new_branch_stripped = strip_conversation(new_branch_json)
        new_tokens = estimate_tokens(new_branch_stripped)

        if is_append:
            print(
                f"    incremental (append-only): {len(new_branch)} new message(s) "
                f"(~{new_tokens:,} tokens) + existing summary "
                f"({len(old_summary.split()):,} words)",
                flush=True,
            )
            branch_instructions = (
                f"# Incremental summary update (append-only)\n\n"
                f"This conversation was previously summarized. New messages have "
                f"been appended at the end. Below is the existing summary followed "
                f"by ONLY the new messages.\n\n"
                f"Your job:\n"
                f"1. Read the existing summary to understand what was already covered\n"
                f"2. Read the new messages\n"
                f"3. Produce a complete updated summary — keep the existing material "
                f"(you may lightly edit for coherence), append new sections for new "
                f"material, and update frontmatter if the new content changes "
                f"significance, categories, or topics\n"
                f"4. If the new messages are trivial (\"thanks\", \"ok\"), keep the "
                f"summary essentially unchanged — just update last_active in "
                f"frontmatter\n\n"
                f"---\n\n"
                f"# Existing summary\n\n{old_summary}\n\n"
                f"---\n\n"
                f"# New messages\n\n{new_branch_stripped}\n"
            )
        else:
            old_branch_json = _format_branch_messages(old_branch)
            old_branch_stripped = strip_conversation(old_branch_json)
            old_tokens = estimate_tokens(old_branch_stripped)
            print(
                f"    incremental (edit): {len(old_branch)} removed / "
                f"{len(new_branch)} added message(s) "
                f"(~{old_tokens:,} / ~{new_tokens:,} tokens) from fork point "
                f"(after {branches.get('common_count', '?')} shared messages) "
                f"+ existing summary ({len(old_summary.split()):,} words)",
                flush=True,
            )
            branch_instructions = (
                f"# Incremental summary update (conversation edited)\n\n"
                f"This conversation was previously summarized, but has since been "
                f"edited. The conversation diverges from the original after message "
                f"{branches.get('common_count', '?')}. Below is the existing "
                f"summary, then the OLD branch (removed messages) and the NEW "
                f"branch (replacement messages).\n\n"
                f"Your job:\n"
                f"1. Read the existing summary\n"
                f"2. Read the OLD branch to identify which parts of the summary "
                f"correspond to removed content — cut or revise those parts\n"
                f"3. Read the NEW branch to identify new material — integrate it\n"
                f"4. Produce a complete updated summary with correct frontmatter\n"
                f"5. If the changes are trivial, keep the summary mostly unchanged\n\n"
                f"---\n\n"
                f"# Existing summary\n\n{old_summary}\n\n"
                f"---\n\n"
                f"# OLD branch (removed messages)\n\n{old_branch_stripped}\n\n"
                f"---\n\n"
                f"# NEW branch (replacement messages)\n\n{new_branch_stripped}\n"
            )

        input_text = (
            f"# Pending work context\n\n{pending_context}\n\n"
            f"---\n\n"
            f"{metrics_block}\n"
            f"---\n\n"
            f"{branch_instructions}"
        )
        try:
            output = run_claude(
                _instruction_file(),
                input_text,
                model=model,
                effort=effort,
            )
        except ClaudeInvocationError as e:
            print(f"  ✗ {uuid[:8]} — claude error (incremental): {e}", flush=True)
            return False
        _delete_branches(uuid, created_at)
    elif not needs_chunking(conv_text, significance=significance):
        # Normal path: single call, full conversation.
        input_text = (
            f"# Pending work context\n\n{pending_context}\n\n"
            f"---\n\n"
            f"{metrics_block}\n"
            f"---\n\n"
            f"# Conversation JSON\n\n{conv_text}\n"
        )
        try:
            output = run_claude(
                _instruction_file(),
                input_text,
                model=model,
                effort=effort,
            )
        except ClaudeInvocationError as e:
            print(f"  ✗ {uuid[:8]} — claude error: {e}", flush=True)
            return False
    else:
        # Sliding window: chunk the conversation, summarize each chunk
        # sequentially. Each segment after the first receives the previous
        # segment's summary as read-only context so it understands references
        # to earlier material. Segments are concatenated at the end — no
        # "stitch" pass that could compress the output.
        #
        # A lightweight final call generates only frontmatter + opening
        # paragraph from the concatenated segments. This is narrow enough
        # that the model doesn't treat it as a summarization opportunity.
        chunks = chunk_messages(conv_text, max_chars=MAX_CHARS)
        n_chunks = len(chunks)
        est_tokens = estimate_tokens(conv_text)
        print(
            f"    (oversized: ~{est_tokens:,} tokens after stripping → "
            f"{n_chunks} chunks, sliding window)",
            flush=True,
        )
        # Segments are cached to disk so a rate-limit hit on a later segment
        # doesn't lose completed work. On retry, cached segments are loaded
        # and only remaining ones are processed.
        from .paths import segments_dir
        seg_cache_dir = segments_dir() / uuid
        seg_cache_dir.mkdir(parents=True, exist_ok=True)

        segment_summaries: list[str] = []
        per_segment_target = max(high_floor // n_chunks, 800)
        for i, chunk in enumerate(chunks, 1):
            seg_file = seg_cache_dir / f"segment_{i}.md"
            if seg_file.exists():
                cached = seg_file.read_text(encoding="utf-8").strip()
                if cached:
                    segment_summaries.append(cached)
                    print(f"    segment {i}/{n_chunks} loaded from cache ({len(cached.split()):,} words)", flush=True)
                    continue

            # Build context block: for segments 2+, include the previous
            # segment's summary so the model understands references to
            # earlier material (terms introduced, decisions made, etc.).
            if i == 1 or not segment_summaries:
                context_block = ""
            else:
                prev_summary = segment_summaries[-1]
                context_block = (
                    f"# Context from previous segment ({i-1}/{n_chunks})\n\n"
                    f"The following is the summary of the previous segment. "
                    f"It is provided ONLY so you understand references to "
                    f"earlier material (terms, decisions, context). Do NOT "
                    f"re-summarize or repeat this content — it will be "
                    f"concatenated separately. Your job is to summarize ONLY "
                    f"the new material in this segment.\n\n"
                    f"{prev_summary}\n\n"
                    f"---\n\n"
                )

            segment_instruction = (
                f"# Note: chunked conversation (segment {i} of {n_chunks})\n\n"
                f"This conversation is too long for a single pass. You are "
                f"summarizing segment {i} of {n_chunks}. Produce a thorough "
                f"summary of ONLY the NEW material in this segment. "
                f"No frontmatter. "
                f"No opening paragraph situating the conversation — just the "
                f"section-by-section coverage of what's in this chunk. Be "
                f"detailed: every distinct idea, frame, decision, coinage. "
                f"These segment summaries will be mechanically concatenated "
                f"at the end (no editing pass), so each segment must stand "
                f"on its own while not repeating earlier segments.\n\n"
                f"**Your segment summary should be at least {per_segment_target:,} "
                f"words.** The full conversation target is {high_floor:,} words "
                f"across {n_chunks} segments. It is better to write too much "
                f"than too little — there is no later pass that adds material, "
                f"so anything you leave out now is lost permanently.\n\n"
                f"---\n\n"
            )
            input_text = (
                f"# Pending work context\n\n{pending_context}\n\n"
                f"---\n\n"
                f"{metrics_block}\n"
                f"---\n\n"
                f"{context_block}"
                f"{segment_instruction}"
                f"# Conversation JSON (segment {i}/{n_chunks})\n\n{chunk}\n"
            )
            try:
                seg = run_claude(
                    _instruction_file(),
                    input_text,
                    model=model,
                    effort=effort,
                )
            except ClaudeInvocationError as e:
                print(f"  ✗ {uuid[:8]} — claude error on segment {i}/{n_chunks}: {e}", flush=True)
                print(f"    ({i - 1} segment(s) cached — re-run to resume)", flush=True)
                return False
            seg_text = seg.strip()
            segment_summaries.append(seg_text)
            seg_file.write_text(seg_text, encoding="utf-8")
            print(f"    segment {i}/{n_chunks} done ({len(seg_text.split()):,} words)", flush=True)

        # Mechanical concatenation — no compression pass.
        concatenated = "\n\n".join(segment_summaries)
        concat_words = len(concatenated.split())
        print(
            f"    concatenated {n_chunks} segments ({concat_words:,} words total)",
            flush=True,
        )

        # Lightweight frontmatter-only pass: the model sees the concatenated
        # segments and produces ONLY the YAML frontmatter block + a short
        # opening paragraph. The body is NOT re-processed.
        frontmatter_input = (
            f"# Pending work context\n\n{pending_context}\n\n"
            f"---\n\n"
            f"{metrics_block}\n"
            f"---\n\n"
            f"# Frontmatter generation for a chunked conversation\n\n"
            f"Below are concatenated segment summaries of a single "
            f"conversation. Your ONLY job is to produce:\n\n"
            f"1. The standard frontmatter YAML block (title, uuid, "
            f"created_at, last_active, project, categories, topics, "
            f"keywords, significance) — opened and closed with `---`\n"
            f"2. A short opening paragraph (2-4 sentences) situating the "
            f"conversation — what it was about and what was at stake\n\n"
            f"Output NOTHING else. Do not summarize, do not reproduce the "
            f"segment content, do not add a Specifics section. Just the "
            f"frontmatter block and the opening paragraph. The segment "
            f"bodies will be appended mechanically after your output.\n\n"
            f"---\n\n"
            f"# Segment summaries (for context only — do not reproduce)\n\n"
            f"{concatenated}\n"
        )
        try:
            frontmatter_output = run_claude(
                _instruction_file(),
                frontmatter_input,
                model="haiku",
            )
        except ClaudeInvocationError as e:
            print(f"  ✗ {uuid[:8]} — claude error on frontmatter pass: {e}", flush=True)
            print(f"    (segments cached in {seg_cache_dir} — re-run to retry)", flush=True)
            return False

        # Combine: frontmatter + opening paragraph + concatenated segments.
        # The frontmatter output should end with the opening paragraph.
        # Strip any trailing whitespace, then append the segment bodies.
        fm_text = frontmatter_output.strip()
        output = fm_text + "\n\n" + concatenated
        print(f"    done ({len(output.split()):,} words total)", flush=True)

        # Success — clean up segment cache.
        import shutil
        shutil.rmtree(seg_cache_dir, ignore_errors=True)

    # Write summary. Month derived from created_at so summaries mirror
    # conversations/ layout.
    month = (conv_meta.get("created_at") or "unknown")[:7]
    out_dir = summaries_dir() / month
    out_dir.mkdir(parents=True, exist_ok=True)
    # Reuse existing summary filename if present (survives title edits).
    existing_sum = conv_meta.get("summary_file")
    if existing_sum and not existing_sum.startswith("summaries/deleted/"):
        out_path = data_root() / existing_sum
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_path = out_dir / f"{stem_for(uuid, conv_meta.get('title'), conv_meta.get('created_at'))}.md"
    # Down-link FIRST: the raw conversation JSON this summary was made from.
    # It's part of the body, so it must be in place before we measure —
    # otherwise the stored word count and a later recompute-metrics (which
    # measures the final body) would disagree. The parent (up) link is NOT
    # written here — it depends on the sparse-month merge decision only
    # synthesize knows, so synthesize stamps it later.
    from .links import set_full_conversation
    output = set_full_conversation(output, conv_meta["conversation_file"])
    # Measure the BODY only (prose, frontmatter excluded). The frontmatter
    # holds the metrics themselves, so counting it would make the numbers
    # self-referential — and it keeps `recompute-metrics` byte-stable, since
    # it can re-derive the exact same value from any later file state.
    from .metrics import split_frontmatter
    summary_metrics = measure_text(split_frontmatter(output)[1])
    ratio = compression_ratio(summary_metrics["chars"], orig_chars)
    used_model = model or "sonnet"
    output = _inject_metrics(output, orig_words, summary_metrics, ratio, used_model, identity={
        "uuid": uuid,
        "title": conv_meta.get("title"),
        "created_at": conv_meta.get("created_at"),
        "updated_at": conv_meta.get("updated_at"),
        "project_name": conv_meta.get("project_name"),
        "conversation_model": conv_model,
    })
    # Inject a stats subheader as the first body line so the reader sees
    # key numbers at a glance without opening frontmatter.
    output = _inject_summary_subheader(output, orig_words, summary_metrics["words"], ratio, conv_model or "")
    # Atomic write: never leave a half-finished summary on disk if something
    # crashes between bytes. Write to a sibling .tmp and rename on success.
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(output, encoding="utf-8")
    os.replace(tmp_path, out_path)

    rel = str(out_path.relative_to(data_root()))
    conv_meta["summary_file"] = rel
    conv_meta["summary_chars"] = summary_metrics["chars"]
    conv_meta["summary_words"] = summary_metrics["words"]
    conv_meta["summary_tokens_est"] = summary_metrics["tokens_est"]
    conv_meta["compression_ratio"] = ratio
    conv_meta["model"] = used_model
    conv_meta["summarized_at"] = now_iso()

    # Pull significance from the frontmatter Claude just wrote and store it
    # in state so `chronicle ls` can show it without re-reading every file.
    from .metrics import parse_frontmatter
    fm = parse_frontmatter(output)
    sig = fm.get("significance")
    if sig:
        conv_meta["significance"] = sig

    # Clean up branch file if it wasn't already consumed by the incremental path.
    _delete_branches(uuid, created_at)

    print(f"  ✓ {uuid[:8]} — done → {rel}", flush=True)
    return True


def run(args: Any) -> None:
    ensure_dirs()
    state = state_mod.load()

    # Reconcile state with disk before deciding what's stale: if a summary's
    # .md file was deleted (or moved), drop the freshness marker so the work
    # gets redone. Without this, `summarized_at` could lie about reality.
    reset = state_mod.reconcile_summaries(state)
    if reset:
        state_mod.save(state)
        print(
            f"Reconciled state: {len(reset)} summary file(s) missing on disk, "
            f"marked stale.",
            flush=True,
        )

    try:
        targets, in_scope, scope_desc = _select_targets(state, args)
    except SystemExit:
        raise

    # --dry-run: show the queue (what would be summarized) and stop, before
    # any state mutation or Claude call. Mirrors `chronicle syn --dry-run`.
    if getattr(args, "dry_run", False):
        if not targets:
            print(f"Nothing to summarize in {scope_desc} — all fresh.")
            return
        print(
            f"{len(targets)} conversation(s) to summarize in {scope_desc}, in order:"
        )
        rows = sorted(
            ((u, state["conversations"].get(u, {})) for u in targets),
            key=lambda x: x[1].get("created_at") or "",
        )
        for uuid, c in rows:
            date = (c.get("created_at") or "")[:10]
            title = (c.get("title") or "(untitled)")[:60]
            print(f"  · {date}  {title}  [{uuid[:8]}]")
        print("\nDry run — nothing summarized. Drop --dry-run to run them.")
        return

    force = getattr(args, "force", False)
    if force and targets:
        # Clear summarized_at so summarize_one writes to the existing path
        # cleanly (no leftover stale state) and reconcile doesn't fight us.
        # We *keep* `significance` in state — it's the prior pass's judgment
        # about how this conversation should be summarized, and it informs
        # the chunking decision for the re-run (high-sig + large → force
        # chunking to dodge the output ceiling). If the prior pass got it
        # wrong, the re-run will overwrite it with whatever the model
        # decides this time.
        from pathlib import Path
        for uuid in targets:
            c = state["conversations"][uuid]
            sf = c.get("summary_file")
            if sf:
                p = data_root() / sf
                if p.exists():
                    p.unlink()
            for k in ("summarized_at", "summary_chars", "summary_words",
                      "summary_tokens_est", "compression_ratio"):
                c.pop(k, None)
        state_mod.save(state)
        print(f"Force mode: cleared {len(targets)} existing summary/ies.", flush=True)

    if not targets:
        if in_scope == 0:
            print(
                f"No conversations found in {scope_desc}. "
                f"Nothing was ingested for that scope — check `chronicle stale` "
                f"or widen the range."
            )
        else:
            print(
                f"All {in_scope} conversation(s) in {scope_desc} are already "
                f"summarized and fresh (summarized_at ≥ updated_at). "
                f"To force a re-summary: use -f / --force, or delete the .md "
                f"file under data/summaries/ and re-run."
            )
        return

    # Fail fast if claude isn't installed before we start processing.
    from shutil import which
    if not which("claude"):
        raise SystemExit(
            "`claude` binary not found on $PATH. Install Claude Code "
            "(https://claude.com/claude-code) and ensure `claude --version` works, "
            "then re-run."
        )

    pending_context = _read_pending_context()
    workers = max(1, int(getattr(args, "workers", 1) or 1))
    model = getattr(args, "model", None) or "sonnet"
    effort = getattr(args, "effort", None) or "low"
    print(
        f"Summarizing {len(targets)} conversation(s). "
        f"Model: {model} · effort: {effort} · workers: {workers}"
    )
    succeeded = 0
    failed = 0
    state_lock = threading.Lock()

    def _task(uuid: str) -> bool:
        # summarize_one mutates state["conversations"][uuid] — that's a
        # different key per task, so concurrent mutation is safe. The lock
        # only guards the save.
        ok = summarize_one(
            uuid, state, pending_context=pending_context,
            model=model, effort=effort,
        )
        if ok:
            with state_lock:
                state_mod.save(state)
        return ok

    if workers == 1:
        for uuid in targets:
            if _task(uuid):
                succeeded += 1
            else:
                failed += 1
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_task, u): u for u in targets}
            for fut in as_completed(futures):
                if fut.result():
                    succeeded += 1
                else:
                    failed += 1

    # One final rewrite of pending.md so processed UUIDs fall off.
    pending_mod.write_pending(state)

    # Rebuild search index after successful work.
    if succeeded > 0:
        from .index import build_index
        build_index(state)

    print(f"\nDone. {succeeded} ok, {failed} failed.")
    if failed:
        print("Re-run `chronicle summarize --uuid <uuid>` to retry specific failures.")
