@echo off
REM Run the JobHighLander scraper on a loop with a random 1-3h gap between runs.
cd /d "%~dp0"
call venv\Scripts\activate.bat
python scheduler.py indeed
