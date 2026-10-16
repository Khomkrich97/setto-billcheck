@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
rem Meta ad charge check (ad team). First run installs Python + tools (about 5-10 minutes).
rem The program files are copied to %LOCALAPPDATA%\NISIT\billcheck\app so "start with Windows" keeps
rem working even if this folder is moved; every START refreshes that copy.
set "BASE=%LOCALAPPDATA%\NISIT\billcheck"
set "VENV=%BASE%\venv"
set "VPY=%VENV%\Scripts\python.exe"
set "VPYW=%VENV%\Scripts\pythonw.exe"
set "APPD=%BASE%\app"
set "DEPS=playwright==1.61.0 pymupdf==1.28.0"
rem Updates come from github.com/Khomkrich97/setto-billcheck by themselves (app.py checks on every start).
rem A git clone is a developer copy: no auto-update, so unpushed changes are not overwritten.
if exist "%~dp0.git" set "BILLCHECK_NO_UPDATE=1"
if exist "%VENV%\ready.txt" goto run

echo === First run: installing tools (one time only, needs internet) ===
set "PY="
for %%P in ("%LOCALAPPDATA%\Programs\Python\Python312\python.exe" "%ProgramFiles%\Python312\python.exe") do (
  if not defined PY if exist %%P set "PY=%%~P"
)
if not defined PY (
  where py >nul 2>nul && for /f "delims=" %%i in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do set "PY=%%i"
)
if not defined PY (
  echo Installing Python 3.12 ...
  powershell -NoProfile -ExecutionPolicy Bypass -Command "$ProgressPreference='SilentlyContinue'; Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe' -OutFile \"$env:TEMP\python-3.12.10-amd64.exe\""
  if not exist "%TEMP%\python-3.12.10-amd64.exe" goto fail
  "%TEMP%\python-3.12.10-amd64.exe" /quiet InstallAllUsers=0 PrependPath=0 Include_launcher=0 Include_test=0
  set "PY=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
)
if not exist "%PY%" goto fail

"%PY%" -m venv "%VENV%" || goto fail
"%VPY%" -m pip install --disable-pip-version-check -q %DEPS% || goto fail
"%VPY%" -m playwright install chromium || goto fail
> "%VENV%\ready.txt" echo %DEPS%
echo === Install done ===
echo.

:run
if not exist "%~dp0app\app.py" goto nofiles
robocopy "%~dp0app" "%APPD%" /E /PURGE /NFL /NDL /NJH /NJS /NP >nul
if errorlevel 8 goto fail
if not exist "%VPYW%" set "VPYW=%VPY%"
rem app.py puts a shortcut with the program's icon next to this file (and once on the desktop)
set "BILLCHECK_FROM=%~dp0"
start "" "%VPYW%" "%APPD%\app.py"
exit /b 0

:nofiles
echo.
echo Folder "app" is missing next to START.bat. Unzip the whole zip first.
pause
exit /b 1

:fail
echo.
echo Install failed. Take a screenshot of this window and send it to Master.
pause
exit /b 1
