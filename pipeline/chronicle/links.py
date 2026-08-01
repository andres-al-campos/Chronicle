"""Obsidian wikilink emission for tree navigation.

Every file in the fractal tree gets two kinds of link, Wikipedia-style:

  - parent ("up"), as the FIRST body line:
      summary  : (none at write time — synthesize stamps it; see below)
      entry    : `Part of [[<parent entry>]]`  (year has no parent)
  - children ("down"), as a section at the BOTTOM of the body:
      summary  : `## Full conversation` → [[<uuid>.json]]
      entry    : `## Sources`           → [[<child stem>]] bullets

Wikilinks resolve by basename. Markdown targets are linked WITHOUT the
`.md` extension (`[[2026_Q2_Entry]]`); the conversation JSON is not
markdown so it keeps its extension (`[[abc.json]]`) — Obsidian treats it
as a non-rendered leaf, which is fine.

Parent links on *summaries* are deliberately NOT written by summarize:
the parent label depends on the sparse-month merge decision, which only
synthesize knows. synthesize stamps `Part of [[…]]` into each child it
consumes, so the link is always correct (incl. the merged H1-H2 case)
and self-heals on re-synthesis. See `set_parent_link`.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from .metrics import render_with_frontmatter, split_frontmatter

_PARENT_PREFIX = "Part of [["
_FULL_CONVO_HEADING = "## Full conversation"
_SOURCES_HEADING = "## Sources"


def _stem(name: str) -> str:
    """Basename without a trailing .md (Obsidian resolves md by stem)."""
    return name[:-3] if name.endswith(".md") else name


def wikilink_md(rel_or_name: str) -> str:
    """`[[stem]]` for a markdown target (path or bare name accepted)."""
    return f"[[{_stem(Path(rel_or_name).name)}]]"


def wikilink_file(rel_or_name: str) -> str:
    """`[[name.ext]]` for a non-markdown target (keeps extension)."""
    return f"[[{Path(rel_or_name).name}]]"


def _strip_existing_parent(body: str) -> str:
    """Drop a leading `Part of [[…]]` line (and the blank after it) so the
    parent link can be re-stamped idempotently / corrected on re-synthesis."""
    lines = body.split("\n")
    if lines and lines[0].startswith(_PARENT_PREFIX):
        i = 1
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        return "\n".join(lines[i:])
    return body


def set_parent_link(text: str, parent_md_name: str) -> str:
    """Return `text` with `Part of [[parent]]` as the first body line.
    Idempotent: replaces any existing parent line rather than stacking.
    Frontmatter is preserved exactly (parse → reserialize, never splice)."""
    fields, body = split_frontmatter(text)
    body = _strip_existing_parent(body)
    new_body = f"Part of {wikilink_md(parent_md_name)}\n\n{body}"
    if fields:
        return render_with_frontmatter(fields, new_body)
    return new_body


def _drop_section(body: str, heading: str) -> str:
    """Remove a trailing `---`-fenced `## heading` block (and the one we
    add) so the down-section can be regenerated idempotently."""
    lines = body.rstrip().split("\n")
    for i, ln in enumerate(lines):
        if ln.strip() == heading:
            cut = i
            # also swallow a `---` separator immediately above it
            j = i - 1
            while j >= 0 and lines[j].strip() == "":
                j -= 1
            if j >= 0 and lines[j].strip() == "---":
                cut = j
            return "\n".join(lines[:cut]).rstrip()
    return body.rstrip()


def set_down_section(text: str, heading: str, link_lines: list[str]) -> str:
    """Replace/append a bottom `## heading` section of wikilink bullets.
    `link_lines` are already-formatted bullet strings (without leading
    `- `). Idempotent: an existing section with this heading is removed
    first. Frontmatter preserved via parse → reserialize."""
    fields, body = split_frontmatter(text)
    base = _drop_section(body, heading)
    bullets = "\n".join(f"- {l}" for l in link_lines)
    new_body = f"{base}\n\n---\n\n{heading}\n\n{bullets}\n"
    if fields:
        return render_with_frontmatter(fields, new_body)
    return new_body


def set_full_conversation(text: str, conversation_rel: str) -> str:
    """Bottom `## Full conversation` section linking the raw JSON leaf."""
    return set_down_section(
        text, _FULL_CONVO_HEADING, [wikilink_file(conversation_rel)]
    )


def set_sources(text: str, child_md_names: list[str]) -> str:
    """Bottom `## Sources` section of wikilinks to child entries/summaries."""
    return set_down_section(
        text, _SOURCES_HEADING, [wikilink_md(n) for n in child_md_names]
    )


# ---------------------------------------------------------------------------
# Inline link fixer — resolves short-UUID wikilinks to full slugs
# ---------------------------------------------------------------------------

# Matches [[target]] or [[target|alias]] where target looks like a bare
# 8-char hex UUID (not already a full slug with underscores/hyphens before it).
_WIKILINK_RE = re.compile(r"\[\[([^\]|]+?)(?:\|([^\]]+?))?\]\]")

# A "short UUID" is 8+ hex chars with no underscores or path separators —
# i.e. the model wrote just the UUID portion, not the full slug.
_SHORT_UUID_RE = re.compile(r"^[0-9a-f]{8,}$", re.IGNORECASE)

# A "period label" is what the model writes when it references another entry
# by its calendar period rather than its filename: 2026_Q1, 2026-03-H1,
# 2026_Apr_H2, 2026_04_H1-H2, 2026. Accepts the accepted-input variants
# (underscore/hyphen separators, abbreviated or numeric months) — canonical_label
# normalizes them before lookup. A bare year (2026) is included; four bare digits
# are unambiguous as a period here. We deliberately don't try to match anything
# looser, so ordinary slug wikilinks (which contain a headline) pass through.
_PERIOD_LABEL_RE = re.compile(
    r"^\d{4}"                                  # year
    r"(?:[_-](?:Q[1-4]|[A-Z][a-z]{2}|0[1-9]|1[0-2])"  # _Q2 / _Apr / _04
    r"(?:[_-]H[12](?:-H2)?)?"                   # optional _H1 / _H2 / _H1-H2
    r")?$",
    re.IGNORECASE,
)


def _build_uuid_to_slug(state: dict[str, Any]) -> dict[str, str]:
    """Build a lookup from UUID (or UUID prefix) → full slug stem.

    Handles both full UUIDs (keys in state["conversations"]) and the
    short 8-char suffixes that appear in slug filenames.
    """
    mapping: dict[str, str] = {}
    for uuid, conv in state.get("conversations", {}).items():
        sf = conv.get("summary_file")
        if not sf:
            continue
        slug = Path(sf).stem  # e.g. "16_hermes-jun-15-stock-split__bf3c3bbf"
        # Map both the full UUID and the short suffix (last 8 chars).
        mapping[uuid] = slug
        short = uuid.replace("-", "")[:8]
        # Only set if not already claimed (first wins on collision, unlikely).
        mapping.setdefault(short, slug)
        # Also map the last segment after the last hyphen in the UUID.
        tail = uuid.rsplit("-", 1)[-1]
        mapping.setdefault(tail, slug)
    return mapping


def fix_inline_links(text: str, state: dict[str, Any]) -> str:
    """Replace short-UUID wikilinks with full-slug versions.

    Scans ``text`` for ``[[<target>]]`` or ``[[<target>|<alias>]]`` where
    ``<target>`` matches the short-UUID pattern. Looks the UUID up in
    ``state["conversations"]`` and rewrites it to the full slug stem.

    Returns the (possibly modified) text and prints warnings for any
    unresolvable short-UUID links.
    """
    uuid_to_slug = _build_uuid_to_slug(state)
    unresolved: list[str] = []

    def _replace(m: re.Match) -> str:
        target = m.group(1).strip()
        alias = m.group(2)

        if not _SHORT_UUID_RE.match(target):
            # Already a full slug or some other valid link — leave it alone.
            return m.group(0)

        slug = uuid_to_slug.get(target.lower())
        if slug is None:
            unresolved.append(target)
            return m.group(0)

        if alias:
            return f"[[{slug}|{alias}]]"
        return f"[[{slug}]]"

    result = _WIKILINK_RE.sub(_replace, text)

    if unresolved:
        print(f"  ⚠  {len(unresolved)} unresolved short-UUID link(s):")
        for u in unresolved:
            print(f"      [[{u}]]")

    fixed = len(_SHORT_UUID_RE.findall(text)) - len(unresolved)
    # Only report if we actually changed something.
    changed = result != text
    if changed:
        # Count how many were fixed by diffing
        orig_short = [m for m in _WIKILINK_RE.finditer(text) if _SHORT_UUID_RE.match(m.group(1).strip())]
        new_short = [m for m in _WIKILINK_RE.finditer(result) if _SHORT_UUID_RE.match(m.group(1).strip())]
        n_fixed = len(orig_short) - len(new_short)
        if n_fixed > 0:
            print(f"  ✓  Fixed {n_fixed} short-UUID link(s) → full slugs")

    return result


def _build_period_to_stem(state: dict[str, Any]) -> dict[str, str]:
    """Build a lookup from canonical period label → entry-file stem.

    Entry files are named with a date-prefixed, headline slug
    (`2026-Q1_quitting-and-building-frameworks_Entry`), but the model writes
    the bare period label (`2026_Q1`) when it cross-references another entry.
    Keys are canonical labels (the same form that keys `state["entries"]`),
    so a target normalized via `canonical_label` resolves regardless of the
    separator or month form the model happened to use.
    """
    from .calendar import PeriodParseError, canonical_label

    mapping: dict[str, str] = {}
    for label, entry in state.get("entries", {}).items():
        ef = entry.get("entry_file")
        if not ef:
            continue
        stem = Path(ef).stem  # e.g. "2026-Q1_quitting-and-building-frameworks_Entry"
        try:
            canon = canonical_label(label)
        except PeriodParseError:
            canon = label
        mapping[canon] = stem
    return mapping


def fix_period_links(text: str, state: dict[str, Any]) -> str:
    """Replace bare period-label wikilinks with the target entry's slug stem.

    The period sibling of `fix_inline_links`. Scans for ``[[<target>]]`` or
    ``[[<target>|<alias>]]`` where ``<target>`` is a calendar period label
    (``2026_Q1``, ``2026-03-H1``, ``2026_Apr_H2`` …). Looks the canonicalized
    label up in ``state["entries"]`` and rewrites it to the entry-file stem so
    the wikilink actually resolves in Obsidian. Non-period targets — full
    slugs, UUIDs, headings — are left untouched. Warns on any period label
    that has no matching entry.
    """
    from .calendar import PeriodParseError, canonical_label

    period_to_stem = _build_period_to_stem(state)
    unresolved: list[str] = []
    fixed = 0

    def _replace(m: re.Match) -> str:
        nonlocal fixed
        target = m.group(1).strip()
        alias = m.group(2)

        if not _PERIOD_LABEL_RE.match(target):
            return m.group(0)  # not a period label — leave alone

        try:
            canon = canonical_label(target)
        except PeriodParseError:
            # Shape looked like a label but doesn't parse (e.g. month 13) —
            # not something we can resolve; leave it and don't warn.
            return m.group(0)

        stem = period_to_stem.get(canon)
        if stem is None:
            unresolved.append(target)
            return m.group(0)

        # Already pointing at the right file (target was the stem's period
        # portion and happens to equal it) — the stem differs from a bare
        # label, so this only no-ops when nothing needs changing.
        if stem == target:
            return m.group(0)

        fixed += 1
        if alias:
            return f"[[{stem}|{alias}]]"
        return f"[[{stem}]]"

    result = _WIKILINK_RE.sub(_replace, text)

    if unresolved:
        print(f"  ⚠  {len(unresolved)} unresolved period link(s) (no such entry):")
        for u in unresolved:
            print(f"      [[{u}]]")
    if fixed:
        print(f"  ✓  Fixed {fixed} period link(s) → entry slugs")

    return result
