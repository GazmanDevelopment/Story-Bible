# Story Bible (demo)

Word task-pane add-in plus a small FastAPI server for keeping a story bible (characters, places, relationships, timeline) per series.

- **PLAN.md**: architecture, decisions, build phases, TrueNAS deployment, sideloading
- **[docs/BACKUP.md](docs/BACKUP.md)** / **[docs/RESTORE.md](docs/RESTORE.md)**: what backs up automatically and how to restore it
- `app/`: FastAPI backend (`main.py`) and task pane (`static/`)
- `manifest.dev.xml` / `manifest.prod.xml`: Word add-in manifests, generated from `manifest.template.xml` by `scripts/generate_manifest.py` (#8) - edit the template, not the generated files
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
