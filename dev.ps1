# Windows'ta "make dev" karşılığı: backend + Vite arayüzü (http://127.0.0.1:5273)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Push-Location "$root\backend"; uv sync; uv run python -m app.init_env; Pop-Location   # sistem anahtarları (.env) yoksa üretilir
Push-Location "$root\frontend"; if (-not (Test-Path node_modules)) { npm install }; Pop-Location
$backend = Start-Process -PassThru -NoNewWindow -WorkingDirectory "$root\backend" uv `
  -ArgumentList "run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8765", "--reload", "--reload-dir", "app"
try {
  Push-Location "$root\frontend"
  npm run dev
} finally {
  Pop-Location
  if ($backend -and -not $backend.HasExited) { taskkill /PID $backend.Id /T /F | Out-Null }
}
