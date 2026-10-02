@echo off
cd /d "%~dp0"
python -m ifc_linked_repair
if errorlevel 1 pause
