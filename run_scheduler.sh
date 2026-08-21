#!/usr/bin/env bash
# Launch scheduler.py as an unprivileged user, under a virtual X display.
#
# Chrome must run HEADED here: scraper/browser.py documents that headless Chrome
# is always detectable without a patched Chromium, so a headless run will not
# clear Indeed/Glassdoor's Cloudflare challenge. This box has no GUI, so Xvfb
# supplies a real display for a real (headful) Chrome window.
#
# It must also run UNPRIVILEGED. pm2 starts this wrapper as root, and Chrome
# refuses to run as root unless you pass --no-sandbox — the one flag
# scraper/browser.py deliberately keeps OFF the command line, because a real
# browser never carries it (see the chromium_sandbox comment there). Dropping to
# a normal account is what lets the sandbox stay on: Chrome then uses its setuid
# helper, /opt/google/chrome/chrome-sandbox, which is also the only sandbox
# available on this box — Ubuntu 24.04 denies unprivileged user namespaces via
# kernel.apparmor_restrict_unprivileged_userns.
#
# Symptom if this drop is ever removed: every Chrome-backed site fails at once.
# patchright reports the real cause ("Running as root without --no-sandbox is
# not supported"), while site_login.launch_chrome sends Chrome's stderr to
# DEVNULL and so reports only its 30s timeout, "Chrome did not open a debugging
# port" — which reads like a stale profile and is not.
set -euo pipefail

SELF=$(readlink -f "$0")
cd "$(dirname "$SELF")"

RUN_AS=${SCRAPER_USER:-jhscraper}

if [ "$(id -u)" -eq 0 ]; then
  RUN_AS_HOME=$(getent passwd "$RUN_AS" | cut -d: -f6)
  [ -n "$RUN_AS_HOME" ] || { echo "run_scheduler.sh: no such user '$RUN_AS'" >&2; exit 1; }

  # Re-assert ownership of the writable paths on every start: a deploy runs
  # `git reset --hard` as root, and a fresh checkout has no screenshots/ at all,
  # so without this the scraper comes back up unable to write its own profiles.
  install -d -o "$RUN_AS" -g "$RUN_AS" sessions logs screenshots
  install -d -o "$RUN_AS" -g "$RUN_AS" -m 700 "$RUN_AS_HOME/run"

  # .env holds the DB and proxy credentials. Keep it off world-read, but let
  # $RUN_AS read it — re-created by hand it would default to root-only 600 and
  # fail deep inside config.py rather than here.
  if [ -f .env ]; then
    chgrp "$RUN_AS" .env
    chmod 640 .env
  fi

  # setpriv keeps the caller's environment, so hand over by name the variables
  # that have to follow the uid: Chrome writes under both $HOME and
  # $XDG_RUNTIME_DIR, and root's /run/user/0 is not ours to write.
  exec setpriv --reuid="$RUN_AS" --regid="$RUN_AS" --init-groups \
    env HOME="$RUN_AS_HOME" \
        USER="$RUN_AS" \
        LOGNAME="$RUN_AS" \
        XDG_RUNTIME_DIR="$RUN_AS_HOME/run" \
    bash "$SELF" "$@"
fi

# `xvfb-run -a` picks a free display number and exports DISPLAY for the whole
# process subtree, which matters because scheduler.py launches each scrape as a
# fresh `python main.py <site>` subprocess that must inherit it.
exec xvfb-run -a \
  --server-args="-screen 0 1920x1080x24 -nolisten tcp" \
  ./venv/bin/python -u scheduler.py "${@:-all}"
