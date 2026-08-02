"""Parse export JSON files from data/inbox/ and split into per-conversation files.

For each conversation in the export:
- Write data/conversations/YYYY-MM/{uuid}.json (YYYY-MM from created_at).
- Update state.json with latest updated_at (triggers summary_stale derivation).

For each uuid in export_metadata.deleted_uuids:
- Soft-delete: move the conversation file + any existing summary into
  */deleted/ subdirs (never unlink).
- Set state.conversations[uuid].deleted_at.

After processing: regenerate data/pending.md and fire a macOS notification.
Only the conversation metadata + full JSON body are written; no Claude is called.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import pending as pending_mod
from . import state as state_mod
from .metrics import conversation_prose, measure_text
from .notify import notify
from .paths import (
    branches_dir,
    conversations_dir,
    data_root,
    deleted_conversations_dir,
    deleted_summaries_dir,
    ensure_dirs,
    inbox_dir,
    stem_for,
    summaries_dir,
)
from .state import now_iso


def _month_key(iso_ts: str) -> str:
    # "2026-04-05T09:00:00Z" → "2026-04"
    return iso_ts[:7] if iso_ts else "unknown"


def _write_conversation(
    conv: dict[str, Any], existing_rel: str | None
) -> tuple[Path, int]:
    """Write the conversation JSON and return (path, char_count of the text payload).

    If we've seen this UUID before (existing_rel set), keep the original filename
    even if the title changed — the UUID8 suffix is the stable anchor.
    """
    uuid = conv["uuid"]
    month = _month_key(conv.get("created_at") or "")
    out_dir = conversations_dir() / month
    out_dir.mkdir(parents=True, exist_ok=True)
    if existing_rel:
        out_path = data_root() / existing_rel
        out_path.parent.mkdir(parents=True, exist_ok=True)
    else:
        out_path = out_dir / f"{stem_for(uuid, conv.get('title'), conv.get('created_at'))}.json"
    payload = json.dumps(conv, indent=2, ensure_ascii=False)
    with out_path.open("w", encoding="utf-8") as f:
        f.write(payload)
        f.write("\n")
    return out_path, len(payload)


def _relpath(path: Path) -> str:
    # Store paths relative to data/ for portability. We know paths live under
    # data_root() because we just wrote them there.
    from .paths import data_root
    try:
        return str(path.relative_to(data_root()))
    except ValueError:
        return str(path)


def _message_uuids(conv: dict[str, Any]) -> list[str]:
    """Extract ordered message UUIDs from a conversation."""
    return [m["uuid"] for m in (conv.get("messages") or []) if m.get("uuid")]


def _is_empty_conversation(conv: dict[str, Any]) -> bool:
    """True if a conversation has no substantive content — a blank chat that
    got a UUID and an auto-title but no actual message text.

    claude.ai occasionally records a conversation with an empty `messages`
    array (a new chat opened and abandoned). Tracking it is pure noise: it
    can't be summarized (0 words), so the summarizer emits a placeholder and
    re-summaries stack junk stat-lines. We test prose words, not just the
    message count, so a conversation whose messages carry no text also counts.
    """
    if not (conv.get("messages") or []):
        return True
    return measure_text(conversation_prose(conv))["words"] == 0


def _extract_branches(
    uuid: str, old_path: Path, new_conv: dict[str, Any]
) -> dict[str, Any] | None:
    """Compare old and new conversation, extract divergent branches.

    Instead of storing abstract UUID diffs, we save the actual message
    content from each branch after their fork point. This gives the
    summarizer everything it needs without cross-referencing UUIDs.

    Returns a branch dict if there are changes, None if identical.
    Branch structure:
      - old_branch_messages: messages only in old (from fork point onward)
      - new_branch_messages: messages only in new (from fork point onward)
      - common_count: number of shared prefix messages
      - is_append_only: True if old branch is empty (just new messages added)
    """
    if not old_path.exists():
        return None
    try:
        old_conv = json.loads(old_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None

    old_msgs = old_conv.get("messages") or []
    new_msgs = new_conv.get("messages") or []

    old_uuids = [m.get("uuid") for m in old_msgs]
    new_uuids = [m.get("uuid") for m in new_msgs]

    # Find the shared prefix length (messages in common at the start).
    common_count = 0
    for o, n in zip(old_uuids, new_uuids):
        if o == n:
            common_count += 1
        else:
            break

    old_tail = old_msgs[common_count:]
    new_tail = new_msgs[common_count:]

    if not old_tail and not new_tail:
        return None  # identical

    is_append_only = len(old_tail) == 0 and len(new_tail) > 0

    return {
        "uuid": uuid,
        "branched_at": now_iso(),
        "common_count": common_count,
        "old_branch_message_count": len(old_tail),
        "new_branch_message_count": len(new_tail),
        "old_branch_messages": old_tail,
        "new_branch_messages": new_tail,
        "is_append_only": is_append_only,
    }


def _write_branches(uuid: str, branches: dict[str, Any], created_at: str) -> None:
    month = _month_key(created_at)
    out_dir = branches_dir() / month
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{uuid}.json"
    out_path.write_text(
        json.dumps(branches, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _soft_delete(uuid: str, state: dict[str, Any]) -> bool:
    """Move conversation + summary files to */deleted/ subdirs. Returns True
    if we actually tombstoned something new."""
    from .paths import data_root

    conv_meta = state["conversations"].get(uuid)
    if not conv_meta:
        return False  # never saw it — nothing to delete
    if conv_meta.get("deleted_at"):
        return False  # already tombstoned

    # Move conversation file.
    conv_rel = conv_meta.get("conversation_file")
    if conv_rel:
        src = data_root() / conv_rel
        if src.exists():
            deleted_conversations_dir().mkdir(parents=True, exist_ok=True)
            dst = deleted_conversations_dir() / f"{uuid}.json"
            shutil.move(str(src), str(dst))
            conv_meta["conversation_file"] = _relpath(dst)

    # Move summary file if any.
    sum_rel = conv_meta.get("summary_file")
    if sum_rel:
        src = data_root() / sum_rel
        if src.exists():
            deleted_summaries_dir().mkdir(parents=True, exist_ok=True)
            dst = deleted_summaries_dir() / f"{uuid}.md"
            shutil.move(str(src), str(dst))
            conv_meta["summary_file"] = _relpath(dst)

    conv_meta["deleted_at"] = now_iso()
    return True


def ingest_export(export_path: Path, state: dict[str, Any]) -> dict[str, list[str]]:
    """Process one export file. Mutates state in place. Returns change lists."""
    with export_path.open("r", encoding="utf-8") as f:
        export = json.load(f)

    added: list[str] = []
    updated: list[str] = []
    deleted: list[str] = []
    unchanged: list[str] = []

    for conv in export.get("conversations", []):
        uuid = conv.get("uuid")
        if not uuid:
            continue

        # Skip genuinely empty conversations (blank chats with no message
        # content). Never track a new one; tombstone one we already tracked
        # (e.g. its content was removed). This runs before any file write, so
        # an empty conversation still present in an export can't be resurrected
        # by the un-tombstone branch below — it never reaches it.
        if _is_empty_conversation(conv):
            if state["conversations"].get(uuid):
                if _soft_delete(uuid, state):
                    deleted.append(uuid)
            continue

        existing = state["conversations"].get(uuid)
        existing_rel = existing.get("conversation_file") if existing else None
        # Don't keep a tombstoned path if the conversation is being re-added.
        if existing_rel and existing_rel.startswith("conversations/deleted/"):
            existing_rel = None

        # Extract divergent branches before overwriting the old file.
        # Only worth doing if we already have a summary (otherwise we'd
        # just do a full summarize anyway).
        if existing_rel and existing and existing.get("summarized_at"):
            old_path = data_root() / existing_rel
            branches = _extract_branches(uuid, old_path, conv)
            if branches:
                _write_branches(uuid, branches, conv.get("created_at") or "")

        out_path, char_count = _write_conversation(conv, existing_rel)
        prose = measure_text(conversation_prose(conv))
        if existing is None:
            state["conversations"][uuid] = {
                "title": conv.get("title"),
                "created_at": conv.get("created_at"),
                "updated_at": conv.get("updated_at"),
                "project_name": conv.get("project_name"),
                "conversation_file": _relpath(out_path),
                "conversation_chars": char_count,
                "original_chars": prose["chars"],
                "original_words": prose["words"],
                "original_tokens_est": prose["tokens_est"],
                "summary_file": None,
                "summary_chars": None,
                "summary_words": None,
                "summary_tokens_est": None,
                "compression_ratio": None,
                "summarized_at": None,
                "deleted_at": None,
                "first_seen": now_iso(),
            }
            added.append(uuid)
        else:
            prev_updated = existing.get("updated_at")
            # Writer stays fresh (conversation_file), title/project can change too.
            existing["title"] = conv.get("title") or existing.get("title")
            existing["project_name"] = conv.get("project_name") or existing.get("project_name")
            existing["updated_at"] = conv.get("updated_at") or prev_updated
            existing["conversation_file"] = _relpath(out_path)
            existing["conversation_chars"] = char_count
            existing["original_chars"] = prose["chars"]
            existing["original_words"] = prose["words"]
            existing["original_tokens_est"] = prose["tokens_est"]
            # If the conversation was previously tombstoned and came back,
            # un-tombstone. (Rare but possible if a user restores one.)
            if existing.get("deleted_at"):
                existing["deleted_at"] = None
            if prev_updated and conv.get("updated_at") and conv["updated_at"] > prev_updated:
                updated.append(uuid)
            else:
                unchanged.append(uuid)

    meta = export.get("export_metadata") or {}
    for uuid in meta.get("deleted_uuids") or []:
        if _soft_delete(uuid, state):
            deleted.append(uuid)

    state["last_ingest"] = now_iso()
    processed = state.setdefault("processed_imports", [])
    if export_path.name not in processed:
        processed.append(export_path.name)
    # Legacy key migration — also update old key if present so nothing breaks.
    if "processed_exports" in state:
        legacy = state["processed_exports"]
        if export_path.name not in legacy:
            legacy.append(export_path.name)

    return {"added": added, "updated": updated, "deleted": deleted, "unchanged": unchanged}


def ingest_all(
    explicit_path: Path | None = None,
    *,
    latest: bool = False,
) -> dict[str, Any]:
    """Ingest export files from data/inbox/.

    - explicit_path: process exactly this file.
    - latest=True: process only the most recently modified file in inbox/.
    - Neither: process every unprocessed file.
    """
    ensure_dirs()
    state = state_mod.load()
    processed = set(
        state.get("processed_imports", []) + state.get("processed_exports", [])
    )

    if explicit_path is not None:
        targets = [explicit_path]
    elif latest:
        candidates = sorted(
            inbox_dir().glob("chronicle-export-*.json"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        targets = [candidates[0]] if candidates else []
    else:
        targets = sorted(
            p for p in inbox_dir().glob("chronicle-export-*.json")
            if p.name not in processed
        )

    totals = {"added": [], "updated": [], "deleted": [], "unchanged": [], "files": []}
    for path in targets:
        changes = ingest_export(path, state)
        totals["added"].extend(changes["added"])
        totals["updated"].extend(changes["updated"])
        totals["deleted"].extend(changes["deleted"])
        totals["unchanged"].extend(changes["unchanged"])
        totals["files"].append(path.name)

    state_mod.save(state)

    # Regenerate pending.md. "newly_added" across this ingest batch; deleted
    # rows are the batch's tombstones.
    counts = pending_mod.write_pending(
        state,
        newly_added=totals["added"],
        newly_deleted=totals["deleted"],
    )

    # Notification.
    if totals["files"]:
        parts = []
        if counts["new"]:
            parts.append(f"{counts['new']} new")
        if counts["updated"]:
            parts.append(f"{counts['updated']} updated")
        if counts["deleted"]:
            parts.append(f"{counts['deleted']} deleted")
        if not parts:
            parts.append("no changes")
        stale_count = counts["updated"] + counts["new"] + counts["awaiting"]
        subtitle = (
            f"{stale_count} summaries stale" if stale_count else "all summaries fresh"
        )
        notify("Chronicle ingest", " · ".join(parts), subtitle=subtitle)

    totals["counts"] = counts
    return totals
