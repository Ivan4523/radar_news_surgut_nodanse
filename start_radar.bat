@echo off
rem Start the News Radar window (Qt). Run from this folder.
cd /d "%~dp0"
if not exist .env echo [warn] .env not found: copy .env.example to .env and fill in the bot token.
python -m pip install -q -r requirements.txt
python radar_gui.py
pause