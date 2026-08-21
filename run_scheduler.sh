#!/usr/bin/env bash
# Launch scheduler.py under a virtual X display.
#
# Chrome must run HEADED here: scraper/browser.py documents that headless Chrome
# is always detectable without a patched Chromium, so a headless run will not
# clear Indeed/Glassdoor's Cloudflare challenge. This box has no GUI, so Xvfb
# supplies a real display for a real (headful) Chrome window.
#
# `xvfb-run -a` picks a free display number and exports DISPLAY for the whole
# process subtree, which matters because scheduler.py launches each scrape as a
# fresh `python main.py <site>` subprocess that must inherit it.
set -euo pipefail
cd "$(dirname "$0")"

exec xvfb-run -a \
  --server-args="-screen 0 1920x1080x24 -nolisten tcp" \
  ./venv/bin/python -u scheduler.py "${@:-all}"
