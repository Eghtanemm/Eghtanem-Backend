# First-time local setup (requires Docker Desktop)
$ErrorActionPreference = "Stop"
Set-Location (Split-Path $PSScriptRoot -Parent)

if (-not (Test-Path .env)) { Copy-Item .env.example .env }
New-Item -ItemType Directory -Force -Path database | Out-Null
if (-not (Test-Path database\database.sqlite)) { New-Item -ItemType File -Path database\database.sqlite | Out-Null }

docker compose build app
docker compose run --rm app composer install --no-interaction --prefer-dist
docker compose run --rm app php artisan key:generate --force
docker compose run --rm app php artisan migrate --seed --force
docker compose run --rm app php artisan db:seed --class=DevSampleSeeder --force

Write-Host "`nSetup complete. Start the server with: docker compose up -d"
