# Story Bible (demo)

Word task-pane add-in plus a small FastAPI server for keeping a story bible (characters, places, relationships, timeline) per series.

- **PLAN.md**: architecture, decisions, build phases, TrueNAS deployment, sideloading
- `app/`: FastAPI backend (`main.py`) and task pane (`static/`)
- `manifest.xml`: Word add-in manifest (points at https://localhost:3000)
- `samples/`: two fictional sample series (import-ready JSON)
- `tests/`: API test (pytest) and headless UI test (Playwright)
- `screenshots/`: the pane at task-pane width

Quick start: `pip install fastapi uvicorn` then `python run_demo.py` and open http://localhost:8765
