# Story Bible - FastAPI + SQLite backend and task-pane static files.
# See PLAN.md for the wider architecture, and issue #1 for what this covers.
# Pinned by digest (#74): a tag like 3.12-slim is a moving target, so two builds
# of the same commit could differ. Dependabot's docker ecosystem proposes new
# digests. To bump by hand: docker buildx imagetools inspect python:3.12-slim
FROM python:3.12-slim@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

# TrueNAS SCALE's "apps" user/group is 568:568. Running as it means a
# dataset created with that ownership (issue #6) just works as /data with
# no chown step needed on the NAS.
RUN groupadd -g 568 storybible && \
    useradd -u 568 -g storybible -M -s /usr/sbin/nologin storybible

WORKDIR /app

COPY requirements.txt .
RUN python -m pip install --no-cache-dir pip==26.2.1 && \
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

# The container runs with a read-only root filesystem (deploy/compose.yaml.example,
# #74), so Python can't write __pycache__ at runtime; compile the bytecode now
# instead so startup isn't slower for it. (Root-owned and world-readable, like
# the rest of /app.)
RUN python -m compileall -q /app/app && chmod -R a+rX /app

# Where the SQLite file (and later, backups - #5) live; see STORYBIBLE_DB in
# app/main.py. Chowned here so a plain `docker run -v vol:/data` (no explicit
# ownership) works too - the real TrueNAS dataset is created as 568:568
# directly (#6), so this doesn't matter for that path.
RUN mkdir -p /data && chown storybible:storybible /data
VOLUME ["/data"]

# Build stamp shown in the pane, the help guide and filed issues (#144). Passed
# by deploy/update.sh and CI (--build-arg); a bare `docker build` reports "dev".
ARG BUILD_VERSION=dev
ARG BUILD_DATE=
ENV BUILD_VERSION=$BUILD_VERSION BUILD_DATE=$BUILD_DATE

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
