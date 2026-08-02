"""Chronicle CLI entry point."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

MODEL_ALIASES: dict[str, str] = {
    "opus5": "claude-opus-5",
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-4-6",
    "haiku": "claude-haiku-4-5-20251001",
}

# Shared effort choices for the summarize/synthesize -e flag.
EFFORT_CHOICES = ["low", "medium", "high", "max"]


def resolve_model(name: str) -> str:
    """Resolve a short alias to a full model ID, or pass through as-is."""
    return MODEL_ALIASES.get(name, name)


def _ingest_cmd(args: argparse.Namespace) -> None:
    from .ingest import ingest_all
    explicit = Path(args.path).expanduser().resolve() if args.path else None
    totals = ingest_all(explicit, latest=getattr(args, "latest", False))
    counts = totals["counts"]
    print(f"Processed {len(totals['files'])} file(s): {', '.join(totals['files']) or '(none)'}")
    print(
        f"  Added:     {len(totals['added'])}\n"
        f"  Updated:   {len(totals['updated'])}\n"
        f"  Unchanged: {len(totals['unchanged'])}\n"
        f"  Deleted:   {len(totals['deleted'])} (tombstoned)"
    )
    print(
        f"Pending: {counts['new']} new · {counts['updated']} updated · "
        f"{counts['awaiting']} awaiting · {counts['stale_entries']} stale entries"
    )


def _sync_cmd(args: argparse.Namespace) -> None:
    """The full loop: ingest the latest export, then summarize everything stale.

    This is the no-arg default — `chronicle` with nothing else runs this. The
    old free ingest-only behavior lives on under `--ingest-only`.
    """
    # 1. Ingest. `--path` targets a specific file; otherwise take the latest.
    args.latest = args.path is None
    _ingest_cmd(args)

    if getattr(args, "ingest_only", False):
        return

    # 2. Summarize every stale conversation (summarize.run also reindexes).
    print()  # blank line between the ingest report and the summarize run
    from . import summarize
    sum_args = argparse.Namespace(
        uuid=None,
        all_stale=True,
        date=None,
        date_now=None,
        force=False,
        dry_run=getattr(args, "dry_run", False),
        workers=getattr(args, "workers", 1),
        model=resolve_model(getattr(args, "model", "opus5")),
        effort=getattr(args, "effort", "low"),
    )
    summarize.run(sum_args)


def _status_cmd(args: argparse.Namespace) -> None:
    from .status import print_status
    print_status()


def _summarize_cmd(args: argparse.Namespace) -> None:
    from . import summarize
    args.model = resolve_model(args.model)
    summarize.run(args)


def _synthesize_cmd(args: argparse.Namespace) -> None:
    from . import synthesize
    args.model = resolve_model(args.model)
    synthesize.run(args)


def _repair_summaries_cmd(args: argparse.Namespace) -> None:
    from . import summarize, state as state_mod
    st = state_mod.load()
    n = summarize.repair_summaries(st, dry_run=getattr(args, "dry_run", False))
    if n == 0:
        print("No malformed summaries found.")
    elif args.dry_run:
        print(f"{n} file(s) would be repaired. "
              "Run without --dry-run, then `chronicle recompute-metrics`.")
    else:
        print(f"Repaired {n} file(s). "
              "Now run `chronicle recompute-metrics` to refresh the numbers.")


def _install_agent_cmd(args: argparse.Namespace) -> None:
    from .agent import install
    install()


def _uninstall_agent_cmd(args: argparse.Namespace) -> None:
    from .agent import uninstall
    uninstall()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="chronicle", description="Chronicle pipeline.")
    sub = parser.add_subparsers(dest="command")

    p_ingest = sub.add_parser("ingest", aliases=["ing"], help="Import conversation exports from data/inbox/.")
    p_ingest.add_argument("path", nargs="?", help="Specific file to ingest. Default: every unprocessed file in data/inbox/.")
    p_ingest.add_argument("-l", "--latest", action="store_true", help="Process only the most recent file in data/inbox/ (by modification time).")
    p_ingest.set_defaults(func=_ingest_cmd)

    p_sync = sub.add_parser(
        "sync", aliases=["sy"],
        help="The full loop (also the no-arg default): ingest the latest export, "
        "then summarize every stale conversation and rebuild the index. "
        "Calls Claude and costs money — use --ingest-only for the old free "
        "ingest, or --dry-run to preview what would be summarized.",
    )
    p_sync.add_argument("path", nargs="?", help="Specific export to ingest. Default: the latest unprocessed file in data/inbox/.")
    p_sync.add_argument("-i", "--ingest-only", action="store_true", help="Stop after ingest — no summarize, no Claude, no spend (the old default).")
    p_sync.add_argument("-n", "--dry-run", action="store_true", help="Ingest, then list the conversations that would be summarized without calling Claude.")
    p_sync.add_argument("-w", "--workers", type=int, default=1, help="Parallel claude invocations for the summarize pass (default 1; try 4 for bulk).")
    p_sync.add_argument("-m", "--model", default="opus5", help="Model alias (opus5, opus, sonnet, haiku) or full ID for the summarize pass. Default: opus5.")
    p_sync.add_argument("-e", "--effort", default="low", choices=EFFORT_CHOICES, help="Extended thinking effort level for the summarize pass (default: low).")
    p_sync.set_defaults(func=_sync_cmd)

    p_status = sub.add_parser("status", aliases=["sts"], help="Print pipeline state.")
    p_status.set_defaults(func=_status_cmd)

    p_sum = sub.add_parser("summarize", aliases=["sum"], help="Run Claude over conversations to generate summaries.")
    sum_target = p_sum.add_mutually_exclusive_group()
    sum_target.add_argument("-u", "--uuid", help="Summarize a single conversation by UUID.")
    sum_target.add_argument("-a", "--all-stale", action="store_true", help="Default. Summarize every stale conversation.")
    sum_target.add_argument("-d", "--date", nargs="+", help="One date/period label (2026-04-22, 2026_Apr_H1, 2026_Apr, 2026_Q2, 2026), or two single-day values for an inclusive range. The literal 'now' = today, so -d 2026-03-20 now covers from the 20th to today.")
    sum_target.add_argument("-dn", "--date-now", metavar="DATE", help="Shortcut for `-d DATE now`: summarize stale conversations from DATE through today. e.g. -dn 2026-03-20.")
    p_sum.add_argument("-f", "--force", action="store_true", help="Force re-summarize even if already fresh. Deletes existing summary file(s) first.")
    p_sum.add_argument("-n", "--dry-run", action="store_true", help="List the conversations that would be summarized, in order, without calling Claude.")
    p_sum.add_argument("-w", "--workers", type=int, default=1, help="Parallel claude invocations (default 1; try 4 for bulk runs). Watch for API rate limits.")
    p_sum.add_argument("-m", "--model", default="opus5", help="Model alias (opus5, opus, sonnet, haiku) or full ID. Resolved to exact ID for provenance. Default: opus5.")
    p_sum.add_argument("-e", "--effort", default="low", choices=EFFORT_CHOICES, help="Extended thinking effort level (default: low).")
    p_sum.set_defaults(func=_summarize_cmd)

    p_syn = sub.add_parser(
        "synthesize", aliases=["syn"],
        help="Build a period entry. Tier inferred from the label "
        "(2026_Apr_H1=half, 2026_Apr=full-month half, 2026_Q2=quarter, 2026=year). "
        "If a month is sparse (<10 conversations), a half-tier run on either "
        "half auto-merges into a single 2026_Apr_H1-H2 entry covering the whole month. "
        "With no --period, synthesizes every missing/stale entry whose period has "
        "already ended, bottom-up (halves → quarters → years).",
    )
    p_syn.add_argument(
        "-p", "--period",
        help="Period label. Examples: 2026_Apr_H1, 2026_Apr_H2, 2026_Apr_H1-H2, 2026_Apr, 2026_Q2, 2026. "
        "Omit to synthesize all complete, out-of-date periods automatically.",
    )
    p_syn.add_argument(
        "-n", "--dry-run", action="store_true",
        help="With no --period: list the periods that would be synthesized, in order, without running Claude.",
    )
    p_syn.add_argument("-m", "--model", default="opus5", help="Model alias (opus5, opus, sonnet, haiku) or full ID. Resolved to exact ID for provenance. Default: opus5.")
    p_syn.add_argument("-e", "--effort", default="low", choices=EFFORT_CHOICES, help="Extended thinking effort level (default: low).")
    p_syn.add_argument("-y", "--yes", action="store_true", help="Skip confirmation prompts (period not yet ended, stale summaries).")
    p_syn.set_defaults(func=_synthesize_cmd)

    p_cap = sub.add_parser(
        "capacity", aliases=["cap"],
        help="Preview what a synthesize call would pack (char count, tokens, missing children).",
    )
    p_cap.add_argument("period", help="Period label (same format as synthesize --period).")
    p_cap.set_defaults(func=lambda a: __import__("chronicle.capacity", fromlist=["run"]).run(a))

    p_ls = sub.add_parser(
        "ls",
        help="Tabular view of conversations with orig/summary word counts and compression ratio.",
    )
    p_ls.add_argument(
        "-p", "--period",
        help="Scope to a period label (e.g. 2026_Mar_H2, 2026_Q1, 2026). Default: all.",
    )
    p_ls.add_argument(
        "-s", "--significance",
        help="Filter by significance: high, medium (or med), low.",
    )
    p_ls.add_argument(
        "-e", "--entries", action="store_true",
        help="Show synthesized entries (tree view) instead of conversations.",
    )
    p_ls.add_argument(
        "--stubs", action="store_true",
        help="Include no-activity stub entries (only with --entries).",
    )
    p_ls.set_defaults(func=lambda a: __import__("chronicle.ls", fromlist=["run"]).run(a))

    p_find = sub.add_parser(
        "find", aliases=["f"],
        help="Search conversations by keyword, topic, or text content.",
    )
    p_find.add_argument("query", nargs="+", help="Search terms (matched against keywords, topics, title via inverted index).")
    p_find.add_argument("-p", "--period", help="Scope search to a period label.")
    p_find.add_argument("-s", "--significance", help="Filter by significance: high, medium (or med), low.")
    p_find.add_argument("--body", action="store_true", help="Also search summary body text (slower, default: index only).")
    p_find.add_argument("-n", "--limit", type=int, default=20, help="Max results to show (default: 20).")
    p_find.set_defaults(func=lambda a: __import__("chronicle.find", fromlist=["run"]).run(a))

    p_index = sub.add_parser(
        "index", aliases=["idx"],
        help="Rebuild the search index (data/index.json). Auto-runs after summarize.",
    )
    p_index.set_defaults(func=lambda a: __import__("chronicle.index", fromlist=["run"]).run(a))

    p_bkw = sub.add_parser(
        "backfill-keywords", aliases=["bkw"],
        help="Extract keywords from existing summaries (one-time migration from tags).",
    )
    p_bkw.add_argument("-f", "--force", action="store_true", help="Re-extract even if keywords already exist.")
    p_bkw.add_argument("-w", "--workers", type=int, default=3, help="Parallel workers (default: 3).")
    p_bkw.add_argument("-m", "--model", default="haiku", help="Model for keyword extraction (default: haiku).")
    def _bkw_cmd(a: argparse.Namespace) -> None:
        a.model = resolve_model(a.model)
        __import__("chronicle.backfill_keywords", fromlist=["run"]).run(a)
    p_bkw.set_defaults(func=_bkw_cmd)

    p_stale = sub.add_parser(
        "stale", aliases=["stl"],
        help="List stale summaries grouped by date, with copy-paste summarize commands.",
    )
    p_stale.add_argument(
        "-p", "--period",
        help="Optional period label to scope the list (e.g. 2026_Mar_H2, 2026_Q1, 2026, 2026-03-22). Default: all.",
    )
    p_stale.set_defaults(func=lambda a: __import__("chronicle.stale", fromlist=["run"]).run(a))

    p_rcm = sub.add_parser(
        "recompute-metrics", aliases=["rcm"],
        help="Recompute word/char/ratio metrics from current file content "
        "(after hand-editing a summary/entry). No Claude call. Idempotent. "
        "synthesize runs this for its children automatically.",
    )
    p_rcm.set_defaults(func=lambda a: __import__("chronicle.recompute_metrics", fromlist=["run"]).run(a))

    p_rep = sub.add_parser(
        "repair-summaries", aliases=["rep"],
        help="Reconstruct any summary whose body got swallowed into its "
        "frontmatter (malformed model output). Structure only, no Claude "
        "call — run recompute-metrics after to refresh the numbers.",
    )
    p_rep.add_argument("-n", "--dry-run", action="store_true",
                       help="List files that would be repaired without writing.")
    p_rep.set_defaults(func=_repair_summaries_cmd)

    p_bfl = sub.add_parser(
        "backfill-links", aliases=["bfl"],
        help="Retrofit Obsidian navigation wikilinks (Full conversation / "
        "Sources / parent links) onto existing summaries+entries. Idempotent. "
        "summarize/synthesize emit links themselves going forward.",
    )
    p_bfl.set_defaults(func=lambda a: __import__("chronicle.backfill_links", fromlist=["run"]).run(a))

    p_rebuild = sub.add_parser(
        "rebuild-state", aliases=["rbs"],
        help="Reconstruct state.json from files on disk. Safety net for corruption/drift.",
    )
    p_rebuild.add_argument("-n", "--dry-run", action="store_true", help="Scan and report but don't overwrite state.json.")
    p_rebuild.add_argument("-c", "--compare", action="store_true", help="Compare existing state.json against what disk says. Implies --dry-run.")
    p_rebuild.set_defaults(func=lambda a: __import__("chronicle.rebuild_state", fromlist=["run"]).run(a))

    p_install = sub.add_parser("install-agent", help="Install launchd agent that auto-ingests new files in data/inbox/.")
    p_install.set_defaults(func=_install_agent_cmd)
    p_uninstall = sub.add_parser("uninstall-agent", help="Remove the launchd agent.")
    p_uninstall.set_defaults(func=_uninstall_agent_cmd)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        # Bare `chronicle` runs the full loop: ingest latest + summarize stale.
        args.path = None
        args.ingest_only = False
        args.dry_run = False
        args.workers = 1
        args.model = "opus5"
        args.effort = "low"
        args.func = _sync_cmd
    try:
        args.func(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
