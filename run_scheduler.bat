@echo off
REM Run the JobHighLander scrapers (Indeed + Glassdoor) on a loop with a random
REM 1-3h gap between runs.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python scheduler.py all
