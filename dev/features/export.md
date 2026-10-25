# Export

Updated 2026-10-25 11:39, at 0.4.0.

The browser extension saves your claude.ai conversations to a JSON file the
pipeline ingests. The same build runs in Chrome and Firefox.

## Sub-features

- **Fetch**: all conversations, or those updated between two dates. Runs in
  the claude.ai tab with your signed-in session, so closing the popup doesn't
  stop it; progress, elapsed time and a log show when the popup reopens.
  Cancel stops it.
- **Only what changed**: conversations not updated since they were last
  downloaded are skipped. Ones that have since disappeared from claude.ai are
  listed in the export's metadata as deleted.
- **Pick and download**: the fetched conversations are listed with title,
  date and word count, changed ones marked. Download saves the checked ones as
  one `chronicle-export-…json`.
- **No claude.ai access**: when the user has taken claude.ai access away, Fetch
  says where to give it back in each browser.

## How to get to it

Install the build (see the README), open claude.ai and sign in, then click
Chronicle Export in the toolbar. Fetch works from any tab while a claude.ai tab
is open.

## Driving it

Preconditions: network access to claude.ai; Firefox in `/Applications`;
Playwright's Chromium (`uvx playwright@1.58.0 install chromium`). No sign-in.

`./build.sh drive export` (~20s): builds, then in headless Chromium and
headless Firefox opens claude.ai's login page and the popup, and checks the
popup shows the manifest's version and reaches the claude.ai tab's content
script the way Fetch does. In Firefox it also checks claude.ai access is
granted at install, then revokes it and checks Fetch says where to allow it.

Fetch and Download against a signed-in account are by hand only: they need a
real account, and a session must not sign in for the user.

Last driven: 2026-10-23, Chromium 145 and Firefox 146. Both passed; with the
MV2 `tabs.executeScript` call put back, both failed.

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
