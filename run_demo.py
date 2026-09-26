"""
Run the Story Bible demo locally.

    pip install fastapi uvicorn
    python run_demo.py            # http://localhost:8765  (browser demo)
    python run_demo.py --https    # https://localhost:3000 (for loading into Word)

--https uses the Office dev certificate. Create it once with:
    npx office-addin-dev-certs install
"""
import argparse
import json
import os
import sqlite3
from pathlib import Path

HERE = Path(__file__).parent
os.environ.setdefault("STORYBIBLE_DB", str(HERE / "data" / "demo.db"))

import uvicorn  # noqa: E402
from app import auth  # noqa: E402
from app.main import app, import_bundle  # noqa: E402  (import creates the DB)


def seed_if_empty() -> None:
    con = sqlite3.connect(os.environ["STORYBIBLE_DB"])
    n = con.execute("SELECT COUNT(*) FROM series").fetchone()[0]
    con.close()
    if n == 0:
        for f in sorted((HERE / "samples").glob("*.json")):
            print("seeding", f.name)
            # Calling the route function directly, bypassing FastAPI's own
            # request handling - its `user` param needs a real CurrentUser,
            # not the Depends(...) sentinel it'd otherwise be left as.
            import_bundle(json.loads(f.read_text()), user=auth.LOCAL_USER)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--https", action="store_true")
    ap.add_argument("--port", type=int)
    a = ap.parse_args()
    seed_if_empty()
    if a.https:
        certs = Path.home() / ".office-addin-dev-certs"
        uvicorn.run(app, host="127.0.0.1", port=a.port or 3000,
                    ssl_certfile=str(certs / "localhost.crt"), ssl_keyfile=str(certs / "localhost.key"))
    else:
        uvicorn.run(app, host="127.0.0.1", port=a.port or 8765)
