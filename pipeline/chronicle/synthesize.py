"""`chronicle synthesize --period <label>` — tiered rollup synthesis.

Tiers and what they read:

  half  (2026_Apr_H1)  reads: fresh summaries of conversations in range.
  half  (2026_Apr_H2)  reads: fresh summaries of conversations in range.
  month (2026_Apr)     reads: the 2 half entries (H1 + H2).
  quarter (2026_Q2)    reads: the 3 month entries.
  year (2026)          reads: the 4 quarter entries.

Rules:
- Refuses if any required child input is missing or stale. Build the lower
  tier first. Token usage stays user-initiated and predictable.
- Budget: max 120k tokens of input per call (conservative against 200k
  context, leaves room for instructions + output). `chars / 4` is the
  token estimate. Single file always allowed.
- No auto-cascading. Run `chronicle synthesize --period 2026_Apr_H1`,
  then `--period 2026_Apr_H2`, then `--period 2026_Apr`, then quarter,
  then year.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from . import pending as pending_mod
from . import state as state_mod
from .calendar import (
    ABBR_TO_MONTH,
    PeriodParseError,
    canonical_merged_label,
    child_tier,
    children_for,
    parse_period,
)
from .claude_invoke import ClaudeInvocationError, run_claude
from .metrics import entry_body, measure_text, parse_frontmatter
from .paths import (
    data_root,
    ensure_dirs,
    entries_dir,
    glossary_file,
    instructions_dir,
    pending_file,
)
from .state import now_iso

CHARS_PER_TOKEN = 4
BUDGET_TOKENS = 120_000

# Below this many non-deleted conversations in a month, halves auto-merge
# into a single 2026_Apr_H1-H2 entry. Two thin halves rarely compress well;
# one merged entry reads better and the quarter rollup costs less.
SPARSE_MONTH_THRESHOLD = 10


def _instruction_file() -> Path:
    return instructions_dir() / "synthesize.txt"


def _extract_headline(output: str) -> str | None:
    """Extract a `headline: ...` directive from the first line of Claude's output."""
    first_line = output.split("\n", 1)[0].strip()
    if first_line.lower().startswith("headline:"):
        return first_line.split(":", 1)[1].strip()
    return None


def _period_to_date_prefix(period: str) -> str:
    """Convert a period label to a YYYY-MM-style sortable prefix.

    2026_04_H1     → 2026-04-H1
    2026_04_H1-H2  → 2026-04-H1-H2
    2026_04        → 2026-04
    2026_Q2        → 2026-Q2
    2026           → 2026

    Also accepts legacy abbreviated form (2026_Apr_H1).
    """
    parts = period.split("_")
    year = parts[0]
    if len(parts) == 1:
        return year  # yearly
    month_part = parts[1]
    # Quarter?
    if month_part.startswith("Q"):
        return f"{year}-{month_part}"
    # Month: numeric ("04") or abbreviated ("Apr")
    month_num = ABBR_TO_MONTH.get(month_part)
    if not month_num:
        try:
            month_num = int(month_part)
        except ValueError:
            return period  # fallback
    prefix = f"{year}-{month_num:02d}"
    if len(parts) >= 3:
        prefix += f"-{parts[2]}"  # H1, H2, H1-H2
    return prefix


def _entry_subdir(period: str) -> str:
    """Return the year/quarter subdirectory for an entry.

    Halves live inside their quarter folder:    2025/Q2/
    Quarters live inside their quarter folder:  2025/Q2/
    Years live at the year level:               2025/
    """
    parts = period.split("_")
    year = parts[0]
    if len(parts) == 1:
        return year  # yearly → entries/YYYY/
    month_part = parts[1]
    if month_part.startswith("Q"):
        q = month_part
        return f"{year}/{q}"  # quarterly → entries/YYYY/Qn/
    # month_part is numeric ("04") or abbreviated ("Apr")
    month_num = ABBR_TO_MONTH.get(month_part)
    if not month_num:
        try:
            month_num = int(month_part)
        except ValueError:
            return year  # fallback
    q = (month_num - 1) // 3 + 1
    return f"{year}/Q{q}"


def entry_filepath(period: str, headline: str | None = None) -> str:
    """Build the entry path relative to entries_dir().

    Includes year/quarter subdirectory:
      2025_05_H1  → 2025/Q2/2025-05-H1_bitcoin-defense_Entry.md
      2025_Q2     → 2025/Q2/2025-Q2_volatility_Entry.md
      2025        → 2025/2025_the-year-of-building_Entry.md
    """
    from .paths import slugify
    prefix = _period_to_date_prefix(period)
    if headline:
        slug = slugify(headline, max_len=50)
        name = f"{prefix}_{slug}_Entry.md"
    else:
        name = f"{prefix}_Entry.md"
    subdir = _entry_subdir(period)
    if subdir:
        return f"{subdir}/{name}"
    return name


def _estimate_tokens(char_count: int) -> int:
    return char_count // CHARS_PER_TOKEN


# ────────────────── label classification + auto-merge ──────────────────

def _half_label_kind(label: str) -> str | None:
    """Classify a half-tier label. Accepts both abbr and numeric month forms.
    Returns one of:
      "half_h1"  — '2026_Apr_H1' / '2026_04_H1'
      "half_h2"  — '2026_Apr_H2' / '2026_04_H2'
      "merged"   — '2026_Apr_H1-H2' / '2026_Apr' / '2026_04_H1-H2' / '2026_04'
      None       — not a half-tier label
    """
    import re as _re
    MON = r"(?:[A-Z][a-z]{2}|0[1-9]|1[0-2])"
    if _re.fullmatch(rf"\d{{4}}_{MON}_H1", label):
        return "half_h1"
    if _re.fullmatch(rf"\d{{4}}_{MON}_H2", label):
        return "half_h2"
    if _re.fullmatch(rf"\d{{4}}_{MON}_H1-H2", label):
        return "merged"
    if _re.fullmatch(rf"\d{{4}}_{MON}", label):
        return "merged"
    return None


def _month_year_num(label: str) -> tuple[int, int]:
    """Extract (year, month_number) from any half-tier label.
    Accepts both numeric (2026_04_H1) and abbreviated (2026_Apr_H1) forms."""
    import re as _re
    m = _re.match(r"(\d{4})_(0[1-9]|1[0-2])", label)
    if m:
        return int(m.group(1)), int(m.group(2))
    m = _re.match(r"(\d{4})_([A-Z][a-z]{2})", label)
    if m:
        return int(m.group(1)), ABBR_TO_MONTH[m.group(2)]
    raise PeriodParseError(f"Cannot extract year/month from '{label}'")


def _month_conversation_count(state: dict[str, Any], year: int, month: int) -> int:
    """Count non-deleted conversations whose created_at falls in this month."""
    _t, rs, re_ = parse_period(f"{year}_{month:02d}")
    return len(state_mod.conversations_in_period(state, rs, re_))


def _resolve_half_label(
    state: dict[str, Any], requested: str
) -> tuple[str, str | None]:
    """Apply auto-merge rules. Given the user-requested half label, return
    (canonical_label, info_message_for_user_or_None).

    Rules:
    - If requested is H1 or H2 and the month has < threshold conversations,
      redirect to the canonical merged label (year_Mon_H1-H2). Print why.
    - If requested is merged (H1-H2 or month alias) and the month has
      >= threshold, refuse — tell the user to run halves separately.
    - Otherwise pass through unchanged. The 2026_Apr alias normalizes to
      2026_Apr_H1-H2 so state and filenames are consistent.
    """
    kind = _half_label_kind(requested)
    if kind is None:
        return requested, None  # non-half label, leave alone

    year, month = _month_year_num(requested)
    count = _month_conversation_count(state, year, month)
    canonical_merged = canonical_merged_label(year, month)
    mm = f"{month:02d}"

    if kind in ("half_h1", "half_h2"):
        if count < SPARSE_MONTH_THRESHOLD:
            msg = (
                f"{year}_{mm} has {count} conversations (< {SPARSE_MONTH_THRESHOLD}). "
                f"Auto-merging halves into a single entry: {canonical_merged}."
            )
            return canonical_merged, msg
        return requested, None

    # kind == "merged" — either explicit H1-H2 or the bare month alias
    if count >= SPARSE_MONTH_THRESHOLD:
        # Refuse the merged form when the month has enough material for halves.
        raise SystemExit(
            f"{year}_{mm} has {count} conversations (>= {SPARSE_MONTH_THRESHOLD}). "
            f"Run the halves separately:\n"
            f"  chronicle synthesize --period {year}_{mm}_H1\n"
            f"  chronicle synthesize --period {year}_{mm}_H2\n"
            f"The merged form is for sparse months only."
        )
    # Sparse + explicitly merged: normalize the alias 2026_04 to 2026_04_H1-H2.
    if requested != canonical_merged:
        return canonical_merged, (
            f"Normalizing '{requested}' to canonical '{canonical_merged}'."
        )
    return canonical_merged, None



def _build_entry_metrics(
    items: list[dict[str, Any]], output: str
) -> dict[str, Any]:
    """Compute the 5 entry-level metrics from the source items + Claude's
    output. Returned dict is keyed for direct frontmatter injection.

    `sources_compression_ratio` = total_summary / total_original.
    This weights by actual size — a true "how compressed is this whole
    pile" number. Averaging per-conversation ratios would weight each
    conversation equally regardless of size, which inflates the number
    when a small conversation has a high ratio.
    """
    total_orig = sum(i.get("source_metrics", {}).get("original_words", 0) for i in items)
    total_sum = sum(i.get("source_metrics", {}).get("summary_words", 0) for i in items)
    aggregate_ratio = (
        round(total_sum / total_orig, 4) if total_orig else 0.0
    )
    body = entry_body(output)
    entry_words = measure_text(body)["words"]
    entry_ratio = (
        round(entry_words / total_sum, 4) if total_sum else 0.0
    )
    return {
        "sources_conversation_words": total_orig,
        "sources_summary_words": total_sum,
        "sources_compression_ratio": aggregate_ratio,
        "entry_words": entry_words,
        "entry_compression_ratio": entry_ratio,
    }


def _inject_entry_metrics(output: str, metrics: dict[str, Any]) -> str:
    """Merge entry-level metrics into the frontmatter. Entries usually open
    with `# ... Entry` (no frontmatter), so split_frontmatter returns an
    empty dict and we build a fresh block. If Claude did emit frontmatter,
    its keys are preserved in order and metrics merged in. Parse-and-
    reserialize, never splice — a body `---` can't be misread as a fence."""
    from .metrics import render_with_frontmatter, split_frontmatter

    fields, body = split_frontmatter(output)
    fields.update(metrics)
    return render_with_frontmatter(fields, body)


_SUBHEADER_RE = re.compile(
    r"^(\*\*\d+\s+(?:conversations?|halves|half-entries|half-month\s+inputs|quarters|inputs)(?:\s*/\s*\d+\s+conversations)?)(?:\s+·.*?)?\*\*",
    re.MULTILINE,
)


def _enrich_subheader(output: str, entry_metrics: dict[str, Any]) -> str:
    """Append source→entry word count and compression ratio to the bold subheader.

    Matches ``**N conversations**`` and rewrites to
    ``**N conversations · X,XXX words → Y,YYY words · 0.XXXX ratio**``.
    """
    sw = entry_metrics.get("sources_summary_words", 0)
    ew = entry_metrics.get("entry_words", 0)
    er = entry_metrics.get("entry_compression_ratio", 0)
    suffix = f" · {sw:,} words → {ew:,} words · {er:.4f} ratio"

    def _replace(m: re.Match) -> str:
        return f"{m.group(1)}{suffix}**"

    return _SUBHEADER_RE.sub(_replace, output, count=1)


def _write_empty_stub(args: Any, tier: str, range_start: str, range_end: str,
                      state: dict[str, Any]) -> None:
    """Write a minimal stub entry for a period with zero conversations.

    No Claude call. The stub exists so higher-tier rollups see a complete
    set of children and don't refuse to build. The rollup tier can mention
    that this period was quiet.
    """
    from .metrics import render_with_frontmatter

    body = (
        f"# {args.period} Entry\n"
        f"**0 conversations**\n\n"
        f"---\n\n"
        f"*No conversations in this period.*\n\n"
        f"---\n"
        f"*Entry closed: {range_end}*\n"
    )
    fields = {
        "period": args.period,
        "tier": tier,
        "range_start": range_start,
        "range_end": range_end,
        "synthesized_at": now_iso(),
        "model": "none",
        "input_count": 0,
        "headline": "No Activity",
        "sources_conversation_words": 0,
        "sources_summary_words": 0,
        "sources_compression_ratio": 0,
        "entry_words": len(body.split()),
        "entry_compression_ratio": 0,
    }
    output = render_with_frontmatter(fields, body)
    out_path = entries_dir() / entry_filepath(args.period, "No Activity")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(output, encoding="utf-8")
    os.replace(tmp_path, out_path)

    entry_record = {
        "tier": tier,
        "entry_file": str(out_path.relative_to(data_root())),
        "synthesized_at": now_iso(),
        "range_start": range_start,
        "range_end": range_end,
        "entry_chars": len(output),
        "is_partial": False,
        "model": "none",
        "headline": "No Activity",
        "sources_conversation_words": 0,
        "sources_summary_words": 0,
        "sources_compression_ratio": 0,
        "entry_words": len(body.split()),
        "entry_compression_ratio": 0,
    }
    state["entries"][args.period] = entry_record
    state_mod.merge_save({"entries": {args.period: entry_record}})

    print(
        f"✓ {args.period} ({tier}) — empty period, stub written → "
        f"{out_path.relative_to(data_root().parent)}"
    )


# ────────────────── input assembly ──────────────────

def _gather_half_inputs(
    state: dict[str, Any], range_start: str, range_end: str
) -> tuple[list[dict[str, Any]], list[str], int]:
    """For a half: every non-deleted conversation's fresh summary in range.
    Returns (input_items, stale_uuids, total_chars).

    Each item carries `source_metrics`: original_words, summary_words,
    compression_ratio. Pulled from state if present, else from summary
    frontmatter, so entry-level rollup metrics are cheap and accurate."""
    convs = state_mod.conversations_in_period(state, range_start, range_end)
    stale = [u for u, c in convs if state_mod.summary_stale(c)]
    items: list[dict[str, Any]] = []
    total_chars = 0
    for uuid, c in convs:
        if state_mod.summary_stale(c):
            continue  # surfaced in `stale` above; caller will refuse
        sum_rel = c.get("summary_file")
        if not sum_rel:
            continue
        sum_path = data_root() / sum_rel
        if not sum_path.exists():
            continue
        text = sum_path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        # State is the preferred source; fall back to frontmatter for legacy
        # files written before state tracked these.
        ow = c.get("original_words") or _to_int(fm.get("original_words"))
        sw = c.get("summary_words") or _to_int(fm.get("summary_words"))
        cr = c.get("compression_ratio")
        if cr is None:
            cr = _to_float(fm.get("compression_ratio"))
        items.append(
            {
                "heading": f"## {c.get('title') or '(untitled)'} — [[{Path(sum_rel).stem}]]",
                "body": text,
                "file_rel": sum_rel,
                "chars": len(text),
                "source_metrics": {
                    "original_words": ow or 0,
                    "summary_words": sw or 0,
                    "compression_ratio": cr or 0.0,
                },
            }
        )
        total_chars += len(text)
    return items, stale, total_chars


def _to_int(v: Any) -> int | None:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _to_float(v: Any) -> float | None:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _resolve_children_with_merges(
    state: dict[str, Any], needed: list[str]
) -> list[str]:
    """Walk the half labels in `needed` and substitute merged H1-H2 entries
    where they exist. If 2026_Apr_H1 and 2026_Apr_H2 are both in `needed`
    and 2026_Apr_H1-H2 exists in state.entries, both are replaced by the
    single merged label. Order is preserved (the merged label takes the
    position of the first half it replaces).
    """
    entries = state.get("entries", {})
    out: list[str] = []
    skip_next_for_month: set[str] = set()  # "2026_Apr" markers
    for child in needed:
        # Only halves can be merged.
        if "_H1" in child or "_H2" in child:
            # Extract the month key, e.g. "2026_Apr"
            month_key = child.split("_H")[0]
            if month_key in skip_next_for_month:
                continue  # already handled by the merged entry
            merged_label = f"{month_key}_H1-H2"
            if merged_label in entries:
                out.append(merged_label)
                skip_next_for_month.add(month_key)
                continue
        out.append(child)
    return out


def _gather_rollup_inputs(
    state: dict[str, Any], label: str
) -> tuple[list[dict[str, Any]], list[str], int]:
    """For quarter/year: the child-tier entries covering the range.

    Quarterly children are the 6 halves of its 3 months. If a month was
    sparse and synthesized as a merged H1-H2 entry, it stands in for both
    of that month's halves — quarter sees 5 (or fewer) inputs cleanly,
    not the same merged file twice.

    Returns (input_items, missing_or_stale_labels, total_chars)."""
    needed = _resolve_children_with_merges(state, children_for(label))
    entries = state.get("entries", {})
    missing: list[str] = []
    items: list[dict[str, Any]] = []
    total_chars = 0
    for child in needed:
        e = entries.get(child)
        if not e:
            missing.append(child + " (missing)")
            continue
        tier, rs, re_ = parse_period(child)
        if state_mod.entry_stale(state, child, rs, re_):
            missing.append(child + " (stale)")
            continue
        entry_path = data_root() / e["entry_file"]
        if not entry_path.exists():
            missing.append(child + " (file missing)")
            continue
        text = entry_path.read_text(encoding="utf-8")
        fm = parse_frontmatter(text)
        # Rollup metrics: pull totals from the child's frontmatter so this
        # entry's metrics roll cleanly. Falls back to 0 if the child predates
        # the metrics injection (older entries).
        items.append(
            {
                "heading": f"## {child}",
                "body": text,
                "file_rel": e["entry_file"],
                "chars": len(text),
                "source_metrics": {
                    # At a rollup tier, the "source" for THIS entry is its
                    # child entries. So:
                    #   original_words = the child's transitive conversation
                    #                    word total (carries through tiers)
                    #   summary_words  = the child entry's own word count
                    #                    (it IS this tier's "summary" input)
                    #   compression_ratio = the child's own entry compression
                    "original_words": _to_int(fm.get("sources_conversation_words") or fm.get("total_source_conversation_words")) or 0,
                    "summary_words": _to_int(fm.get("entry_words")) or 0,
                    "compression_ratio": _to_float(fm.get("entry_compression_ratio")) or 0.0,
                },
            }
        )
        total_chars += len(text)
    return items, missing, total_chars


# ────────────────── batch discovery (no --period) ──────────────────

def _all_candidate_periods(state: dict[str, Any]) -> list[str]:
    """Every half / quarter / year label spanning the conversation history,
    in bottom-up tier order: all halves first, then quarters, then years.

    Halves use the canonical per-month form (YYYY_MM_H1 / _H2). The sparse-
    month auto-merge is applied later, per-period, by _resolve_half_label —
    so we always enumerate both halves and let that layer collapse them.
    """
    from datetime import date as _date

    convs = [
        c for c in state["conversations"].values() if not c.get("deleted_at")
    ]
    dates = [(c.get("created_at") or "")[:10] for c in convs]
    dates = [d for d in dates if d]
    if not dates:
        return []
    first = _date.fromisoformat(min(dates))
    last = _date.fromisoformat(max(dates))

    halves: list[str] = []
    quarters: list[str] = []
    years: list[str] = []
    for year in range(first.year, last.year + 1):
        years.append(str(year))
        for q in range(1, 5):
            quarters.append(f"{year}_Q{q}")
        for month in range(1, 13):
            halves.append(f"{year}_{month:02d}_H1")
            halves.append(f"{year}_{month:02d}_H2")
    # Bottom-up so children exist before parents consume them.
    return halves + quarters + years


def _discover_synthesizable(
    state: dict[str, Any], today: str
) -> list[tuple[str, str]]:
    """Return (canonical_label, tier) for every period that is COMPLETE
    (its range_end is strictly before today) and not-yet-fresh (missing or
    stale), in bottom-up order. Sparse-month halves are collapsed to their
    merged H1-H2 label and de-duplicated. Empty periods are skipped — they
    only need stubs, which the higher tier's gather step writes on demand.
    """
    from .calendar import canonical_label

    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw in _all_candidate_periods(state):
        try:
            label = canonical_label(raw)
            tier, rs, re_ = parse_period(label)
        except PeriodParseError:
            continue

        # Collapse sparse-month halves to the merged label so we don't queue
        # both H1 and H2 for a month that will auto-merge into one entry.
        if tier == "half":
            try:
                merged, _msg = _resolve_half_label(state, label)
                label = merged
                _t2, rs, re_ = parse_period(label)
            except SystemExit:
                # _resolve_half_label refuses a merged form for a dense month;
                # that only happens for explicit merged input, not our per-half
                # enumeration, so treat as non-collapsing and continue.
                pass

        if label in seen:
            continue
        seen.add(label)

        # Only periods that have fully ended. Partial periods are exactly
        # what the user asked to exclude.
        if not (re_ < today):
            continue

        # Skip empty halves entirely — no conversations, nothing to write but
        # a stub, and stubs are created lazily by the parent rollup anyway.
        if tier == "half":
            if not state_mod.conversations_in_period(state, rs, re_):
                continue

        if state_mod.entry_stale(state, label, rs, re_):
            out.append((label, tier))
    return out


def run(args: Any) -> None:
    """Dispatch: a single --period runs once; no --period synthesizes every
    complete, out-of-date period bottom-up."""
    if getattr(args, "period", None):
        _run_one(args)
        return

    ensure_dirs()
    state = state_mod.load()

    from datetime import date as _date
    today = _date.today().isoformat()
    todo = _discover_synthesizable(state, today)

    if not todo:
        print("Nothing to synthesize — all complete periods are up to date.")
        return

    print(f"{len(todo)} period(s) to synthesize (complete + out-of-date), in order:")
    for label, tier in todo:
        print(f"  · {label} ({tier})")
    print()

    if getattr(args, "dry_run", False):
        print("Dry run — nothing synthesized. Drop --dry-run to build them.")
        return

    # Batch mode is non-interactive: every period here has already ended
    # (so no partial-period prompt) and we skip the stale-summary prompt so
    # the run doesn't block. The single-period path is where you get prompts.
    import copy
    failures: list[tuple[str, str]] = []
    for label, _tier in todo:
        one = copy.copy(args)
        one.period = label
        one.yes = True
        print(f"\n{'─' * 60}\n▶ {label}\n{'─' * 60}")
        try:
            _run_one(one)
        except SystemExit as e:
            # A blocked rollup (missing/stale child) or claude error shouldn't
            # abort the whole batch — record it and move on. Later periods that
            # don't depend on it can still succeed.
            failures.append((label, str(e) or "synthesis refused"))
            print(f"⚠ Skipped {label}: {e}")

    print(f"\n{'═' * 60}")
    done = len(todo) - len(failures)
    print(f"Batch complete: {done}/{len(todo)} synthesized.")
    if failures:
        print(f"{len(failures)} skipped:")
        for label, why in failures:
            print(f"  · {label}: {why}")


def _run_one(args: Any) -> None:
    ensure_dirs()
    state = state_mod.load()

    from .calendar import canonical_label
    try:
        tier, _rs0, _re0 = parse_period(args.period)
    except PeriodParseError as e:
        raise SystemExit(str(e))

    # Canonicalize: 2026-03-h1 → 2026_Mar_H1, etc.
    args.period = canonical_label(args.period)

    if tier == "day":
        raise SystemExit(
            f"Day labels (e.g. {args.period}) are summarize-only — synthesis "
            f"starts at the half tier. Try `chronicle synthesize --period "
            f"{args.period[:7].replace('-', '_')}_H1` (or _H2)."
        )

    # Auto-merge / normalize sparse-month half labels. If the user typed
    # 2026_Apr_H1 but April only has 4 conversations, this redirects to
    # 2026_Apr_H1-H2. Also normalizes the 2026_Apr alias to its canonical
    # merged form. For non-half tiers, returns the label unchanged.
    if tier == "half":
        canonical, redirect_msg = _resolve_half_label(state, args.period)
        if redirect_msg:
            print(redirect_msg)
        args.period = canonical

    # Re-parse after possible redirect so range_start/range_end match the
    # canonical label, not what the user typed.
    tier, range_start, range_end = parse_period(args.period)

    # A hand-edited child summary/entry leaves stale word/char/ratio numbers
    # in state + frontmatter, and this rollup sums them. Recompute from
    # current file content first (pure arithmetic, no Claude call) so the
    # totals this entry records are honest. Idempotent — a no-op if nothing
    # was edited.
    from .recompute_metrics import run_recompute
    rc = run_recompute(state)
    if any(rc.values()):
        state_mod.save(state)
        print(
            f"Recomputed drifted metrics before rollup: "
            f"{rc['summary_files']} summary + {rc['entry_files']} entry file(s)."
        )

    skip_prompts = getattr(args, "yes", False)

    # The period is partial if today falls before its end date — more
    # conversations can still land in range. This drives three things: the
    # CLI confirmation below, an `is_partial: true` frontmatter flag, and a
    # prose instruction telling Claude to frame the entry as provisional.
    from datetime import date as _date
    today = _date.today().isoformat()
    is_partial = today < range_end
    if is_partial and not skip_prompts:
        print(
            f"⚠ Period {args.period} ends on {range_end}, but today is {today}.\n"
            f"  Conversations may still be added before the period closes.\n"
            f"  Synthesizing now means you'll likely need to re-synthesize later.\n"
            f"  The entry will be marked is_partial: true (covers through {today})."
        )
        try:
            answer = input("  Continue anyway? [y/N] ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ""
        if answer not in ("y", "yes"):
            print("Aborted.")
            return

    # Idempotency: if a fresh entry already exists at this label, no work.
    existing_entry = state.get("entries", {}).get(args.period)
    if existing_entry and not state_mod.entry_stale(
        state, args.period, range_start, range_end
    ):
        ef = data_root() / existing_entry["entry_file"]
        if ef.exists():
            print(
                f"✓ {args.period} entry already fresh — no work needed.\n"
                f"  {ef.relative_to(data_root().parent)}\n"
                f"  (delete the entry's `synthesized_at` in state.json or wait for "
                f"a conversation update to mark it stale.)"
            )
            return

    if tier == "half":
        items, stale_uuids, total_chars = _gather_half_inputs(
            state, range_start, range_end
        )
        if stale_uuids:
            print(
                f"⚠ {len(stale_uuids)} conversation(s) in {args.period} have stale "
                f"summaries:"
            )
            for u in stale_uuids[:10]:
                c = state["conversations"].get(u, {})
                t = (c.get("title") or "(untitled)")[:50]
                print(f"  · {u[:8]} — {t}")
            if len(stale_uuids) > 10:
                print(f"  … and {len(stale_uuids) - 10} more")
            print(f"\n  Fix with: chronicle sum -d {range_start} {range_end}")
            if not skip_prompts:
                try:
                    answer = input("  Continue without them? [y/N] ").strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = ""
                if answer not in ("y", "yes"):
                    print("Aborted. Summarize first, then re-run synthesize.")
                    return
        if not items:
            _write_empty_stub(args, tier, range_start, range_end, state)
            return
    else:
        items, missing, total_chars = _gather_rollup_inputs(state, args.period)
        if missing:
            below = child_tier(tier)
            print(
                f"Cannot synthesize {args.period}. The following {below} entries "
                f"are missing or stale:"
            )
            for m in missing:
                # `m` looks like "2025_May_H1 (missing)" — extract the label.
                label_only = m.split(" ", 1)[0]
                print(f"  · {m}")
                print(f"      chronicle synthesize --period {label_only}")
            print(
                "\nBuild them in the order shown, then re-run. (Sparse months "
                "auto-merge into a single YYYY_Mon_H1-H2 entry — running either "
                "half label will produce it.)"
            )
            raise SystemExit(1)
        if not items:
            print(f"Nothing to synthesize for {args.period}.")
            return

    tokens_est = _estimate_tokens(total_chars)
    print(
        f"Synthesizing {args.period} ({tier}) · {len(items)} input(s) · "
        f"~{tokens_est:,} tokens"
    )
    if tokens_est > BUDGET_TOKENS and len(items) > 1:
        raise SystemExit(
            f"Input is ~{tokens_est:,} tokens, over the {BUDGET_TOKENS:,} budget. "
            f"Either roll up lower tiers first (a quarter should read 3 month "
            f"entries, not hundreds of summaries) or trim the range."
        )

    pending_text = (
        pending_file().read_text(encoding="utf-8") if pending_file().exists() else ""
    )
    glossary_text = (
        glossary_file().read_text(encoding="utf-8")
        if glossary_file().exists()
        else "(no glossary file present)"
    )
    partial_note = (
        (
            f"# ⚠ PARTIAL PERIOD\n\n"
            f"This {tier} is being synthesized BEFORE it has ended. It covers "
            f"{range_start} through {range_end}, but only conversations up to "
            f"{today} exist so far. Write the entry as explicitly provisional: "
            f"state up front that it is a partial {tier} covering through "
            f"{today}, and frame conclusions as in-progress rather than final. "
            f"This entry will be regenerated once the period closes.\n\n"
            f"---\n\n"
        )
        if is_partial
        else ""
    )
    # For half-tier entries, compute and pass a floor target so the model
    # has a concrete word-count minimum (25% of total summary input).
    # Higher tiers get no floor — their input is already determined.
    if tier == "half":
        total_summary_words = sum(
            it.get("source_metrics", {}).get("summary_words", 0) for it in items
        )
        half_floor = max(int(total_summary_words * 0.25), 500)
        metrics_target = (
            f"# Entry metrics target\n\n"
            f"sources_summary_words: {total_summary_words:,}\n"
            f"minimum_entry_words: {half_floor:,} (25% of source summaries)\n\n"
            f"Before finishing, estimate your entry's word count. If it's below "
            f"{half_floor:,} words, you've likely compressed away material that "
            f"won't survive to the quarterly tier. Exception: if the input "
            f"summaries were thin (few conversations, mostly low-sig), the "
            f"floor doesn't apply — don't pad.\n\n"
            f"---\n\n"
        )
    else:
        metrics_target = ""

    input_text = (
        f"# Glossary (project/term reference — use when matching, leave verbatim otherwise)\n\n"
        f"{glossary_text}\n\n"
        f"---\n\n"
        f"{partial_note}"
        f"# Period to synthesize\n\n"
        f"{args.period} — {tier} — {range_start} → {range_end}\n\n"
        f"{metrics_target}"
        f"# Pending / delta context\n\n{pending_text or '(none)'}\n\n"
        f"---\n\n"
        f"# Inputs ({tier}: {len(items)} {'summary' if tier == 'half' else 'child entry'}{'ies' if len(items) != 1 else ''})\n\n"
        + "\n\n---\n\n".join(f"{it['heading']}\n\n{it['body']}" for it in items)
    )

    model = getattr(args, "model", None) or "claude-opus-5"
    effort = getattr(args, "effort", None) or "low"
    print(f"  Model: {model} · effort: {effort} (extended thinking)")
    try:
        output = run_claude(
            _instruction_file(), input_text,
            model=model,
            effort=effort,
        )
    except ClaudeInvocationError as e:
        print(f"claude error: {e}")
        raise SystemExit(1)

    # Extract the headline directive from the first line.
    headline = _extract_headline(output)
    if headline:
        # Strip the directive line so it doesn't end up in the file.
        output = output.split("\n", 1)[1].lstrip("\n")
    else:
        headline = None

    # Down-links: a `## Sources` section of [[wikilinks]] to every child
    # this entry was synthesized from (summary files for a half, child
    # entries for a rollup). set_sources is idempotent and runs BEFORE
    # metrics injection so the body it measures is the final body.
    from .links import set_sources
    child_names = [it["file_rel"] for it in items]
    output = set_sources(output, child_names)

    # Fix any inline wikilinks where the model wrote a bare UUID instead
    # of the full slug. Runs after set_sources so the Sources section's
    # links (which are already correct) aren't touched — they use stems
    # with underscores, not bare hex UUIDs.
    from .links import fix_inline_links, fix_period_links
    output = fix_inline_links(output, state)
    output = fix_period_links(output, state)

    # Compute and inject the frontmatter BEFORE writing. Two groups:
    # - identity/period metadata (period, tier, range, created_at, input count)
    # - length metrics (the 5 word/ratio fields)
    # Identity comes first so the YAML reads naturally top-down.
    entry_metrics = _build_entry_metrics(items, output)
    identity = {
        "period": args.period,
        "tier": tier,
        "range_start": range_start,
        "range_end": range_end,
        "synthesized_at": now_iso(),
        "model": f"{model} ({effort})",
        "input_count": len(items),
    }
    if is_partial:
        # Only emitted when true. A complete entry simply omits the key, so
        # `fm.get("is_partial")` is falsy and old entries stay valid.
        identity["is_partial"] = "true"
        identity["partial_through"] = today
    if headline:
        identity["headline"] = headline
    output = _inject_entry_metrics(output, {**identity, **entry_metrics})

    # Enrich the subheader (the bold line under # ... Entry) with word
    # count and compression ratio. The model writes "**N conversations**";
    # we append " · X,XXX words · 0.XXXX ratio" so the reader sees the
    # key stats at a glance without opening frontmatter.
    output = _enrich_subheader(output, entry_metrics)

    out_path = entries_dir() / entry_filepath(args.period, headline)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # If re-synthesizing and the headline changed, remove the old file.
    existing_entry = state["entries"].get(args.period)
    if existing_entry and existing_entry.get("entry_file"):
        old_path = data_root() / existing_entry["entry_file"]
        if old_path.exists() and old_path != out_path:
            old_path.unlink()
    # Atomic write: a partially-written entry would poison subsequent rollups
    # (a stale month read of a half-truncated H1 entry, etc).
    tmp_path = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp_path.write_text(output, encoding="utf-8")
    os.replace(tmp_path, out_path)

    entry_record = {
        "tier": tier,
        "entry_file": str(out_path.relative_to(data_root())),
        "synthesized_at": now_iso(),
        "range_start": range_start,
        "range_end": range_end,
        "entry_chars": len(output),
        "is_partial": is_partial,
        "model": f"{model} ({effort})",
        **entry_metrics,
    }
    if tier != "half":
        entry_record["children"] = children_for(args.period)
    state["entries"][args.period] = entry_record

    # Stamp the parent (up) link into every child this entry consumed. We
    # own this link because only synthesize knows the correct parent label
    # — including the sparse-month merged H1-H2 case. Idempotent and
    # self-healing: a re-synthesis (e.g. sparse→merged) rewrites it to the
    # new correct parent. Re-stamping changes each child's body, so its
    # word/ratio metrics drift; run_recompute settles that in one pass
    # (pure arithmetic, no Claude call).
    from .links import set_parent_link
    parent_name = out_path.name
    stamped = 0
    for it in items:
        cp = data_root() / it["file_rel"]
        if not cp.exists():
            continue
        ct = cp.read_text(encoding="utf-8")
        nt = set_parent_link(ct, parent_name)
        if nt != ct:
            ctmp = cp.with_suffix(cp.suffix + ".tmp")
            ctmp.write_text(nt, encoding="utf-8")
            os.replace(ctmp, cp)
            stamped += 1
    if stamped:
        from .recompute_metrics import run_recompute
        run_recompute(state)
        print(f"  Stamped parent link into {stamped} child file(s).")

    # Use merge_save so concurrent synthesize processes don't clobber each
    # other's entries. Each process only owns its own period key.
    state_mod.merge_save({"entries": {args.period: entry_record}})
    # Refresh in-memory state for pending.md generation.
    state = state_mod.load()
    pending_mod.write_pending(state)
    print(f"✓ {args.period} ({tier}) → {out_path.relative_to(data_root().parent)}")
