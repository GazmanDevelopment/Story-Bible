# Story Bible (demo)

Word task-pane add-in plus a small FastAPI server for keeping a story bible (characters, places, relationships, timeline) per series.

- **PLAN.md**: architecture, decisions, build phases, TrueNAS deployment, sideloading
- **[docs/BACKUP.md](docs/BACKUP.md)** / **[docs/RESTORE.md](docs/RESTORE.md)**: what backs up automatically and how to restore it
- **[docs/MONITORING.md](docs/MONITORING.md)**: uptime/backup-freshness alerts and TrueNAS snapshot alerts (#17)
- **[deploy/README.md](deploy/README.md)**: deploying to TrueNAS (#6) - dataset, build, Custom App, reverse proxy
- **[docs/FEEDBACK.md](docs/FEEDBACK.md)**: how the pane's "Log Issue"/"Log Suggestion" buttons work (#22)
- `app/`: FastAPI backend (`main.py`) and task pane (`static/`)
- `manifest.dev.xml` / `manifest.prod.xml`: Word add-in manifests, generated from `manifest.template.xml` by `scripts/generate_manifest.py` (#8) - edit the template, not the generated files
- **[THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)**: licenses for the pip packages this app ships, generated from `requirements.txt` by `scripts/generate_third_party_notices.py` (#66) - vendored front-end libraries (Quill, MSAL.js) have their own notices in `app/static/vendor/*/NOTICE.md` instead
- `samples/`: two fictional sample series (import-ready JSON)
- `tests/`: API test (pytest) and headless UI test (Playwright)
- `screenshots/`: the pane at task-pane width

Quick start: `pip install fastapi uvicorn` then `python run_demo.py` and open http://localhost:8765

## Word showing an old version of the pane?

The server sends `Cache-Control: no-cache` on the task pane's files, so Word
should pick up a new `app.js`/`index.html` on the next reload without any
manual step. If it still looks stale (Word's own WEF cache is unusually
sticky), close Word and clear:

```
%LOCALAPPDATA%\Microsoft\Office\16.0\Wef\
```
