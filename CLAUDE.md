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
  python -m pytest tests/ --ignore=tests/test_ui.py -v
  python -m compileall -q app tests run_demo.py make_samples.py
  ```
  (`tests/test_ui.py` is excluded until it's converted to a real pytest suite -
  see issue #15. If you touch the task pane, run it manually with Playwright
  installed: `python tests/test_ui.py web` and `python tests/test_ui.py word`.)
- These same checks run in CI (`.github/workflows/ci.yml`) against every push
  and PR to `main` - a change with no test coverage or a red local run should
  not be pushed.

**Code review.** Before pushing a change (or opening/updating a PR), run the
`code-review` skill against the diff and address what it finds before it goes
up. Use `/code-review ultra` for anything touching auth, permissions, or
deployment/secrets handling.

**Everything goes through a PR**, even for the repo owner - see the branch
protection notes in issue #19. Work on a feature branch, push it, open a PR,
wait for CI to pass, then merge.
