# Story Bible - FastAPI + SQLite backend and task-pane static files.
# See PLAN.md for the wider architecture, and issue #1 for what this covers.
FROM python:3.12-slim

# TrueNAS SCALE's "apps" user/group is 568:568. Running as it means a
# dataset created with that ownership (issue #6) just works as /data with
# no chown step needed on the NAS.
RUN groupadd -g 568 storybible && \
    useradd -u 568 -g storybible -M -s /usr/sbin/nologin storybible

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
# Docker's COPY preserves the *source* file's permission bits from the
# build context, not just its content - if whatever cloned/pulled this
# repo on the build host did so with a restrictive umask (a real bug on
# an actual TrueNAS deploy, not a hypothetical: PermissionError reading
# app/__init__.py at startup, from the non-root user below), the app code
# ends up unreadable regardless of who owns it. Force a known-good mode
# so this can't happen again, whatever the build host's umask is.
RUN chmod -R a+rX /app

# Where the SQLite file (and later, backups - #5) live; see STORYBIBLE_DB in
# app/main.py. Chowned here so a plain `docker run -v vol:/data` (no explicit
# ownership) works too - the real TrueNAS dataset is created as 568:568
# directly (#6), so this doesn't matter for that path.
RUN mkdir -p /data && chown storybible:storybible /data
VOLUME ["/data"]

USER storybible:storybible
EXPOSE 8000

# Trust X-Forwarded-* only from this address by default (i.e. effectively
# nowhere, since nothing outside the container has it) until the Custom App
# on TrueNAS overrides it with the Synology reverse proxy's LAN IP (#6, #7).
ENV FORWARDED_ALLOW_IPS=127.0.0.1

# No curl in slim, so check with the stdlib instead. /api/health returns a
# real 503 (not just 200 with an "ok": false body) when the DB or /data
# isn't usable (#3), which urlopen raises as HTTPError - caught explicitly
# here rather than relying on an uncaught exception happening to exit 1.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request as u\ntry:\n    sys.exit(0 if u.urlopen('http://127.0.0.1:8000/api/health', timeout=3).status == 200 else 1)\nexcept Exception:\n    sys.exit(1)"]

# SQLite has a single writer - more than one uvicorn worker would just bring
# back the "database is locked" problem the busy_timeout pragma (#2) exists
# to avoid. Scale this by giving the container more resources, not workers.
# `exec` (via sh -c) keeps uvicorn as PID 1's child so it gets SIGTERM
# directly for a clean shutdown, while still letting $FORWARDED_ALLOW_IPS
# expand at container start.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips \"$FORWARDED_ALLOW_IPS\" --no-server-header"]
