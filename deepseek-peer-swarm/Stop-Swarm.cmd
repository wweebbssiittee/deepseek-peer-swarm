@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\Stop-Swarm.ps1"
if errorlevel 1 pause
