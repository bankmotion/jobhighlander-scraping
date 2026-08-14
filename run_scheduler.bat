@echo off
REM Run the JobHighLander scrapers on a loop with a random 1-3h gap between runs.
REM `all` runs the ENABLED sites (see ENABLE_INDEED / ENABLE_GLASSDOOR /
REM ENABLE_JOBRIGHT in .env). Glassdoor is currently disabled there because its
REM Cloudflare challenge needs a proxy IP that can reach brunhild.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python scheduler.py all
