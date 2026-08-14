@echo off
rem Double-click this to install. It sets up a virtual environment, downloads
rem the model, and offers to add a Start menu entry -- no admin rights needed.
rem
rem -ExecutionPolicy Bypass applies to this one process only; it does not
rem change any machine or user policy setting.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0tools\install.ps1"
echo.
pause
