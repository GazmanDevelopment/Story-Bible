"""
Suite-wide test setup.

ENABLE_API_DOCS: app/main.py serves Swagger/ReDoc/openapi.json only when this
is set (off by default since #68). tests/test_openapi.py exercises them, and
pytest loads this file before importing any test module - so setting it here
is early enough to reach app.main's import-time read. The "off by default"
behaviour is tested in a subprocess instead (tests/test_public_surface.py),
since the flag is read once at import.
"""
import os

# Assigned, not setdefault: a stray ENABLE_API_DOCS=false in the developer's
# shell would otherwise fail tests/test_openapi.py for an unrelated reason.
os.environ["ENABLE_API_DOCS"] = "true"
