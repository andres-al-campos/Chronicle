# Chronicle

Chronicle is for looking back at what you've worked through with Claude: what
you decided, what you actually said, and how your thinking changed. It's kept on
your own machine and searchable by you and by Claude.

## Where things are

- `dev/features/README.md`: what a user can do, and the check for each. Read it
  before adding anything.
- `./build.sh check`: the laws in `tests/`. `./build.sh check --drives` adds every drive in `tests/drive/`.
- `README.md` and `pipeline/README.md`: setup and the pipeline's commands.

## Principles

- 2026-10-28 17:45, 0.5.0. **Costly steps stay yours.** What's free runs on its own; what costs money, or
  starts work you didn't ask for, waits for you and tells you when it's worth
  it. Exports stalled on remembering and clicking through, so those should take
  no effort; summaries spend your money, so they never run unattended.
