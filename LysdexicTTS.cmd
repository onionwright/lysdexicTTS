@echo off
rem Double-click this to start the reader.
rem Uses absolute paths so it works from any working directory, and pythonw so
rem no console window appears. It lives in the system tray -- if you don't see
rem the icon, look under the ^ arrow next to the clock.
start "" "%~dp0venv\Scripts\pythonw.exe" "%~dp0run_reader.pyw"
