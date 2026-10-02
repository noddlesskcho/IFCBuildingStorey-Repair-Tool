@echo off
setlocal
cd /d "%~dp0"

rem Find a Python interpreter: PATH first, then the py launcher, then per-user installs.
set "PYEXE="
where python >nul 2>nul && set "PYEXE=python"
if not defined PYEXE (
    where py >nul 2>nul && set "PYEXE=py -3"
)
if not defined PYEXE (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if exist "%%~D\python.exe" set "PYEXE="%%~D\python.exe""
    )
)
if not defined PYEXE (
    for /d %%D in ("%ProgramFiles%\Python3*") do (
        if exist "%%~D\python.exe" set "PYEXE="%%~D\python.exe""
    )
)
if not defined PYEXE (
    echo Python 3 was not found. Install it from https://www.python.org/downloads/
    echo and tick "Add python.exe to PATH" during setup.
    pause
    exit /b 1
)

%PYEXE% -c "import ifcopenshell" >nul 2>nul
if errorlevel 1 (
    echo Installing required packages...
    %PYEXE% -m pip install -r requirements.txt
)

%PYEXE% -m ifc_linked_repair
if errorlevel 1 pause
