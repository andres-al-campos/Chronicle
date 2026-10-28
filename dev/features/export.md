# Export

Updated 2026-10-28 17:42, at 0.5.0.

The browser extension saves your claude.ai conversations to a JSON file the
pipeline ingests. The same build runs in Chrome and Firefox.

## Sub-features

- **Sign-in gate**: the popup asks claude.ai whether you're signed in before it
  shows any controls; until then the space says "Checking claude.ai…". Signed
  out, it says so and offers Sign in, which opens claude.ai's login page and
  closes the popup. When claude.ai can't be reached, sends a Cloudflare
  challenge, or answers in a way Chronicle doesn't recognize, it says that
  instead, since signing in again wouldn't help. A run in progress or a fetched
  list waiting to download skips the gate: downloading needs no sign-in.
- **Fetch**: all conversations, or those updated between two dates. Runs in
  a claude.ai tab with your signed-in session, so closing the popup doesn't
  stop it. With no claude.ai tab open, it opens one behind the current tab and
  waits up to 30s for it to load; a claude.ai tab the browser unloaded to save
  memory, it reloads first; progress, elapsed time and a log show when the popup reopens.
  Cancel stops it.
- **Only what changed**: conversations not updated since they were last
  downloaded are skipped. Ones that have since disappeared from claude.ai are
  listed in the export's metadata as deleted.
- **Pick and download**: the fetched conversations are listed with title,
  date and word count, changed ones marked. Download saves the checked ones as
  one `chronicle-export-…json`.
- **Toolbar badge**: how many conversations changed since your last export, on
  grey (blank at none, "99+" past 99), or a red "!" when you're signed out of
  claude.ai or Chronicle can't read it. Hovering says which. Checked hourly,
  when the browser starts, when you open the popup, and after a download. It
  only lists conversations, with no tab. When claude.ai can't be reached it
  keeps what it last said.
- **Weekly reminder**: a notification once a week saying how many
  conversations changed since your last export, or that you're signed out and
  should sign in first. A week with nothing changed sends none. Clicking it
  opens the popup; nothing opens on its own. If the browser was closed when it
  was due, it comes a minute after the browser starts.
- **No claude.ai access**: when the user has taken claude.ai access away, the
  gate (or Fetch, if it was revoked mid-session) says where to give it back in
  each browser.

## How to get to it

Install the build (see the README) and click Chronicle Export in the toolbar.
Signed out, the popup's Sign in takes you to claude.ai. Fetch works from any tab.

## Driving it

Preconditions: network access to claude.ai; Firefox in `/Applications`;
Playwright's Chromium (`uvx playwright@1.58.0 install chromium`). No sign-in.

`./build.sh drive export` (~20s): builds, then in headless Chromium and
headless Firefox opens claude.ai's login page and the popup, and checks the
popup shows the manifest's version and reaches the claude.ai tab's content
script the way Fetch does. Then it closes every claude.ai tab and checks Fetch
opens one, reaches it, and leaves the popup's tab in front. In Firefox it first
unloads (discards) the claude.ai tab and checks Fetch still reaches it.

The badge, in Chromium with claude.ai's conversation list mocked: "3" with
nothing exported, "2" after marking one as exported unchanged and one as
exported before its latest change, "!" once signed out. In Firefox, signed out
for real, it says "!".

The reminder, run straight from the background (no drive can wait a week or
click a system notification): due 7 days out; in Chromium it says "2
conversations changed", sends nothing once everything is exported, and is due
within a minute when a week overdue. In Firefox, signed out, it says to sign in,
and opening it shows the real toolbar popup. The notification click's wiring to
that function is untested, and so is how the notification looks.

The sign-in gate, in Chromium with claude.ai's `/api/organizations` mocked:
signed out and challenged both hide the controls and read differently, Sign in
opens claude.ai/login, and signed in shows the controls at the gate's height.
In Firefox, against the real (signed-out) claude.ai: access is granted at
install, the gate hides the controls, Sign in opens claude.ai/login when the
answer was "signed out" rather than a challenge, and with access revoked the
gate says where to allow it.

Fetch and Download against a signed-in account are by hand only: they need a
real account, and a session must not sign in for the user. Last by hand:
2026-10-28, Alejandro in Firefox at 0.5.0: the gate let him through and Fetch
worked through a claude.ai tab Firefox had unloaded.

Last driven: 2026-10-28, Chromium 145 and Firefox 146 (Firefox got "signed
out"). Both passed. Both failed with the gate skipped (`openApp()` in place of
`runGate()`), with Fetch giving up when no claude.ai tab is open, with the
tab it opens taking focus, and with signing out leaving the badge as it was.
Firefox failed with an unloaded tab not woken: Fetch got "An unexpected error
occurred", as Alejandro saw in the real popup.
Chromium failed with the reminder sent in a week with nothing changed and with
an overdue reminder waiting a week; Firefox failed with the click opening no
popup (headless Chromium can't show one).
Chromium failed with every conversation counted as changed; Firefox can't see
that, being signed out.

## Gotchas

- Firefox 127 is the first to grant MV3 host access at install. Users can
  still revoke it in about:addons, and Chrome lets them restrict site access.
- `background.service_worker` and `background.scripts` are both declared:
  Chrome uses the first, Firefox the second. `web-ext lint` warns that Firefox
  ignores `service_worker`; that warning is expected.
- `web-ext lint` also warns that `data_collection_permissions` is missing.
  Adding it needs Firefox 140 as the minimum, so it was left out for now.
- The content script still answers an `exportRange` message that the popup
  never sends: a one-step export from before Fetch and Download were split.
- Chrome can't load a zip. The Chrome zip has to be unzipped and loaded
  unpacked; `./build.sh` keeps an unpacked copy in `artifacts/chrome/`.
- `./build.sh` moves other versions' zips into `artifacts/old_versions/` before
  it builds, so `artifacts/` holds only the current version. This version's
  zips stay to be overwritten: archiving them too left a copy of a same-version
  rebuild in `old_versions/`. The old-versions law builds twice to catch that.
- The gate's height is set by hand (`#gate`'s min-height in popup.html) to match
  the fresh setup view, so the popup doesn't resize when the controls replace
  it. The Chromium drive fails when they drift apart.
- Signed out, `/api/organizations` answers 403 JSON (`permission_error`). A
  Cloudflare challenge is also a 403, but HTML; it's the "can't tell" case.
  Signed in, it answers 200 with an array of orgs: no drive sees that, but the
  gate passing in Alejandro's Firefox does.
- Extension pages forbid `eval`, so Playwright's `wait_for_function` can't run
  in the popup; the drive polls with `evaluate`.

## Calls

- 2026-10-27 23:06, 0.4.0. A notification once a week, with no threshold; clicking it opens the popup,
  which never opens on its own (Alejandro: "nothreshold just weekly"). A week
  with nothing changed sends none (Alejandro, 2026-10-27).

- 2026-10-27 20:27, 0.4.0. The badge shows the count of conversations changed since the last export, and
  "!" when signed out (Alejandro, agreeing to it once a signed build was needed
  anyway). Hourly checks and "!" for revoked access too are guesses.
- 2026-10-28 17:28, 0.5.0. The count's badge is a neutral grey, not the popup's purple, which looked odd
  on the icon's orange (Alejandro, 2026-10-28).

- 2026-10-27 20:17, 0.4.0. Fetch opens claude.ai itself when no tab is open (Alejandro, in the plan for
  less sign-in friction). It opens behind, since a focused tab closes the popup
  and its progress; that part is a guess.

- 2026-10-27 19:56, 0.4.0. No controls until the sign-in check passes; the gate uses their space to say
  what's wrong (Alejandro: "i shouldnt even show you the controls until you
  signed in. so it can use that space to notify you"). Applies *Motion spends
  attention*: the popup opens at its final size and the gate's answer fades in.
- 2026-10-27 20:17, 0.4.0. A tab that takes focus closes the popup in both browsers, so anything the
  extension opens while the popup should stay up opens with `active: false`.
- 2026-10-27 20:27, 0.4.0. What counts as changed lives in `changes.js`, loaded by both the content
  script (Fetch) and the background (the badge), so they always agree. It holds
  only function declarations: the content script can be injected into a tab
  twice, and a repeated top-level `const` would throw.
- 2026-10-27 22:41, 0.4.0. Firefox forgets alarms when it quits, so the reminder's due time is kept in
  storage (`lastReminderAt`) and the alarm is set again from it whenever the
  background starts.
- 2026-10-27 22:41, 0.4.0. Firefox 146 opens the toolbar popup from `action.openPopup()` without a user
  gesture, even when the button sits in the Extensions menu. Where it throws,
  the popup opens in a small window instead.
- 2026-10-28 17:28, 0.5.0. A tab the browser unloaded (restored at startup and never clicked, or
  Chrome's Memory Saver) can't be injected into; Firefox says only "An
  unexpected error occurred". Reloading it wakes it, but Firefox first reports
  "complete" for the restored placeholder, before the real page starts loading,
  so the wait holds out for a "loading" first.
- 2026-10-28 17:28, 0.5.0. Discarding a tab in Playwright's headless Chromium takes the browser down, so
  only the Firefox drive covers unloaded tabs.
