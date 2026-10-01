# Story Bible

Word task-pane add-in plus a FastAPI + SQLite server for keeping a per-series story
bible (characters, places, relationships, timeline). See [PLAN.md](PLAN.md) for
architecture, decisions and the build phases, and [README.md](README.md) for a
quick start.

## Required workflow for every code change

**Tests.** Every code change must be backed by a full suite of tests, not just a
test for the new behaviour in isolation:
- Add or update tests covering the change itself (new endpoint, new field,
  changed behaviour, bug fix with a regression test).
- Before pushing, run the whole suite and confirm it's green, not just the new
  test:
  ```
  python -m pytest tests/ -v
  python -m compileall -q app tests scripts run_demo.py make_samples.py
  ```
  (`tests/test_ui.py` is a real pytest suite - `pytest tests/` already
  includes it, spinning up its own server + temp DB. It needs Playwright's
  browser installed once: `playwright install chromium`.)
- These same checks run in CI (`.github/workflows/ci.yml`) against every push
  and PR to `main` - a change with no test coverage or a red local run should
  not be pushed.

**Code review.** Before pushing a change (or opening/updating a PR), run the
`code-review` skill against the diff and address what it finds before it goes
up.

**Everything goes through a PR**, even for the repo owner - see the branch
protection notes in issue #19. Work on a feature branch, push it, open a PR,
wait for CI to pass, then merge.

**User-facing text.** Do not add the GH issue # on the user interface - issue
numbers belong in code comments, commit messages and PR descriptions, not in
anything an end user sees (hints, labels, tooltips, placeholders, toasts).

**Help guide.** Whenever a change affects what the user sees or how the
product works - a UI change, or a change to a flow or behaviour even with no
UI difference - update the help guide (`docs/help/index.html`) to match and
regenerate its screenshots:
```
python scripts/make_help_screenshots.py
```
Commit the updated text and the changed images in `docs/help/img/` with the
change itself, in the same PR. If a new screen or state needs a screenshot,
add it to the script (`tests/test_help_docs.py` checks the guide and script
stay in step). Purely internal changes (refactors, CI, tests) need no guide
update.
