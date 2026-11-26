@echo off
rem Cift tiklayarak calistirin: uygulamayi kurar, baslatir ve tarayicida acar.
rem Kapatmak icin bu pencereyi kapatin.
chcp 65001 >nul
cd /d "%~dp0"
rem uv yeni kurulduysa PATH henuz guncellenmemis olabilir: bilinen kurulum yerlerine de bak
where uv >nul 2>nul
if errorlevel 1 (
  for /d %%D in ("%LOCALAPPDATA%\Microsoft\WinGet\Packages\astral-sh.uv_*") do set "PATH=%%D;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
  if exist "%USERPROFILE%\.local\bin\uv.exe" set "PATH=%USERPROFILE%\.local\bin;%PATH%"
)
where uv >nul 2>nul
if errorlevel 1 (
  echo uv bulunamadi. Kurmak icin:  winget install astral-sh.uv
  pause
  exit /b 1
)
where ffmpeg >nul 2>nul
if errorlevel 1 (
  echo ffmpeg bulunamadi. Kurmak icin:  winget install Gyan.FFmpeg
  pause
  exit /b 1
)
rem Arayuz her acilista derlenir (birkac saniye): kaynak degistiyse eski surum kalmasin
where npm >nul 2>nul
if errorlevel 1 (
  if not exist frontend\dist\index.html (
    echo Node.js bulunamadi. Kurmak icin:  winget install OpenJS.NodeJS.LTS
    pause
    exit /b 1
  )
) else (
  echo Arayuz derleniyor...
  pushd frontend
  if not exist node_modules call npm install
  call npm run build
  popd
)
pushd backend
call uv sync
rem Zorunlu sistem anahtarlari (.env) yoksa uretilir; var olanlara dokunulmaz
call uv run python -m app.init_env
start "" cmd /c "timeout /t 4 >nul & start http://127.0.0.1:8765"
echo.
echo AI Video Studyosu calisiyor: http://127.0.0.1:8765
echo Kapatmak icin bu pencereyi kapatin.
echo.
call uv run uvicorn app.main:app --host 127.0.0.1 --port 8765
popd
pause
