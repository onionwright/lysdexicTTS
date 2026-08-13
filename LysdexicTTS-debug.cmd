@echo off
rem Same app, but with a console window so you can see startup errors.
rem Use this one if the normal launcher seems to do nothing.
echo Starting Kokoro Reader with console output...
echo Log file: %LOCALAPPDATA%\KokoroReader\logs\reader.log
echo.
"%~dp0venv\Scripts\python.exe" "%~dp0run_reader.pyw"
echo.
echo Exited with code %ERRORLEVEL%.
pause
