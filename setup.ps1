# TeamMeetingAI first-run setup.
#
# Prepares everything a new machine needs to run the server:
#   1. Checks prerequisites (Docker running, Python 3.11).
#   2. Creates .env on first run (generates the host token and database
#      password; prompts for the Sarvam/Groq keys).
#   3. Starts the PostgreSQL container (docker compose up -d db).
#   4. Creates .venv and installs dependencies if missing.
#   5. Ensures the database exists and applies migrations (alembic).
#
# The script is idempotent: re-running it never deletes data and only fills
# in what is missing.  An existing .env is kept as-is (missing compose
# variables are appended).
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File setup.ps1
# Unattended (no prompts):
#   powershell -ExecutionPolicy Bypass -File setup.ps1 -SarvamApiKey <key> ``
#       -GroqModel <model> -GroqApiKey <key>
#
# Error handling notes: native tool output (docker, pip, alembic) is checked
# via exit codes; $ErrorActionPreference stays at "Continue" so redirected
# stderr (e.g. `setup.ps1 > log`) cannot masquerade as a terminating error.

param(
    [string]$SarvamApiKey = "",
    [string]$GroqModel = "",
    [string]$GroqApiKey = ""
)

$ErrorActionPreference = "Continue"

# --- helpers -----------------------------------------------------------------

function Write-Step([string]$Message) { Write-Host "==> $Message" }
function Write-Ok([string]$Message) { Write-Host "    OK: $Message" -ForegroundColor DarkGray }
function Write-Fail([string]$Message) { Write-Host "ERROR: $Message" -ForegroundColor Red }

function New-RandomSecret([int]$Length) {
    # Alphanumeric only so it is safe to embed in a DATABASE_URL unescaped.
    $chars = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789".ToCharArray()
    $rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    $bytes = New-Object byte[] $Length
    $rng.GetBytes($bytes)
    $out = New-Object System.Text.StringBuilder
    for ($i = 0; $i -lt $Length; $i++) {
        [void]$out.Append($chars[$bytes[$i] % $chars.Length])
    }
    return $out.ToString()
}

function Import-DotEnvFile([string]$Path) {
    Get-Content -LiteralPath $Path -ErrorAction Stop | ForEach-Object {
        if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
            [Environment]::SetEnvironmentVariable($matches[1], $matches[2].Trim('"'), "Process")
        }
    }
}

function Get-DotEnvValues([string]$Path) {
    $values = @{}
    if (Test-Path -LiteralPath $Path) {
        Get-Content -LiteralPath $Path | ForEach-Object {
            if ($_ -match '^\s*([^#=\s]+)\s*=\s*(.*)$') {
                $values[$matches[1]] = $matches[2].Trim('"')
            }
        }
    }
    return $values
}

function Add-DotEnvEntries([string]$Path, [hashtable]$Entries) {
    $existing = Get-DotEnvValues $Path
    $appended = @()
    foreach ($key in $Entries.Keys) {
        if (-not $existing.ContainsKey($key) -or -not $existing[$key].Trim()) {
            Add-Content -LiteralPath $Path -Value "$key=$($Entries[$key])" -ErrorAction Stop
            $appended += $key
        }
    }
    return $appended
}

function Invoke-Docker {
    # Run docker, returning stdout lines. Callers check $LASTEXITCODE.
    docker @args 2>&1 | Where-Object { $_ -is [string] }
}

function Test-DockerReady {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Fail "Docker is not installed. Install Docker Desktop and retry."
        return $false
    }
    Invoke-Docker info | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Fail "Docker is installed but not running. Start Docker Desktop and retry."
        return $false
    }
    return $true
}

# --- run ---------------------------------------------------------------------

Set-Location -LiteralPath $PSScriptRoot

Write-Step "Checking prerequisites"
if (-not (Test-DockerReady)) { exit 1 }
Write-Ok "Docker is running"

$venvPython = Join-Path (Get-Location) ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $venvPython)) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if (-not $python) {
        Write-Fail "Python was not found. Install Python 3.11 and retry."
        exit 1
    }
    $version = (& python --version 2>&1 | Out-String).Trim() -replace "^Python\s*", ""
    if ($version -notmatch "^3\.11(\.|$)") {
        Write-Host "WARNING: Python $version found; this project targets Python 3.11 (audioop was removed in 3.13)." -ForegroundColor Yellow
    }
    Write-Step "Creating virtual environment"
    python -m venv .venv
    if ($LASTEXITCODE -ne 0) { Write-Fail "python -m venv .venv failed."; exit 1 }
}
Write-Ok "Virtual environment present"

# --- .env --------------------------------------------------------------------

$envFile = Join-Path (Get-Location) ".env"
$generatedHostToken = ""
if (-not (Test-Path -LiteralPath $envFile)) {
    Write-Step "Creating .env (first run)"

    if (-not $SarvamApiKey) {
        $SarvamApiKey = Read-Host "Sarvam API key for transcription (leave blank to add later)"
    }
    if (-not $GroqApiKey) {
        $enableGroq = Read-Host "Enable Groq meeting analysis now? (y/N)"
        if ($enableGroq -match '^(y|yes)$') {
            $GroqModel = Read-Host "Groq model (e.g. openai/gpt-oss-20b)"
            $GroqApiKey = Read-Host "Groq API key"
        }
    }

    $pgUser = "tma"
    $pgDb = "teammeetingai"
    $pgPort = "5433"
    $pgPassword = New-RandomSecret 32
    $generatedHostToken = New-RandomSecret 40
    $databaseUrl = "postgresql+psycopg://${pgUser}:${pgPassword}@localhost:${pgPort}/${pgDb}"

    $lines = [System.Collections.Generic.List[string]]@(
        "# TeamMeetingAI configuration. Never commit this file.",
        "",
        "# --- Database ---"
        "DATABASE_URL=$databaseUrl"
        "TEAMMEETINGAI_PG_USER=$pgUser"
        "TEAMMEETINGAI_PG_PASSWORD=$pgPassword"
        "TEAMMEETINGAI_PG_DB=$pgDb"
        "TEAMMEETINGAI_PG_PORT=$pgPort"
        "",
        "# --- Server ---"
        "TEAMMEETINGAI_HOST_TOKEN=$generatedHostToken"
        "",
        "# --- Transcription ---"
        "TEAMMEETINGAI_SARVAM_API_KEY=$SarvamApiKey"
        ""
    )
    if ($GroqApiKey -and $GroqModel) {
        $lines += @(
            "# --- Analysis ---"
            "TEAMMEETINGAI_ANALYSIS_BACKEND=groq"
            "TEAMMEETINGAI_ALLOW_CLOUD_ANALYSIS=1"
            "TEAMMEETINGAI_GROQ_MODEL=$GroqModel"
            "TEAMMEETINGAI_GROQ_API_KEY=$GroqApiKey"
            ""
        )
    } else {
        $lines += @(
            "# --- Analysis (disabled; meetings stop at transcribed) ---"
            "TEAMMEETINGAI_ANALYSIS_BACKEND=none"
            ""
        )
    }
    $lines += @(
        "# --- Client ---"
        "TEAMMEETINGAI_SERVER_URL=http://localhost:8000"
        ""
    )
    $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
    [System.IO.File]::WriteAllText($envFile, ($lines -join [Environment]::NewLine), $utf8NoBom)
    Write-Ok ".env written"

    Write-Host ""
    Write-Host "Generated secrets (save them somewhere safe; they are not shown again):" -ForegroundColor Yellow
    Write-Host "  Host token:     $generatedHostToken"
    Write-Host "  DB password:    $pgPassword"
    Write-Host ""
} else {
    Write-Step "Using existing .env"
    $appended = Add-DotEnvEntries $envFile @{
        "TEAMMEETINGAI_PG_USER" = "tma"
        "TEAMMEETINGAI_PG_PASSWORD" = (New-RandomSecret 32)
        "TEAMMEETINGAI_PG_DB" = "teammeetingai"
        "TEAMMEETINGAI_PG_PORT" = "5433"
    }
    if ($appended.Count -gt 0) {
        Write-Host "    Appended missing variables: $($appended -join ', ')" -ForegroundColor DarkGray
        Write-Host "    NOTE: if DATABASE_URL has no password, add the new TEAMMEETINGAI_PG_PASSWORD to it before using compose." -ForegroundColor Yellow
    } else {
        Write-Ok ".env complete"
    }
}

Import-DotEnvFile $envFile

# --- database container ------------------------------------------------------

$containerName = "tma-postgres"
$existing = Invoke-Docker ps -a --filter "name=^/$containerName$" --format "{{.Names}}"
if ($existing -eq $containerName) {
    $labelsJson = Invoke-Docker inspect $containerName --format "{{json .Config.Labels}}"
    $isComposeManaged = ($labelsJson -match '"com\.docker\.compose\.project"')
    if ($isComposeManaged) {
        Write-Step "Starting database container"
        docker compose up -d db
        if ($LASTEXITCODE -ne 0) { Write-Fail "docker compose up failed."; exit 1 }
    } else {
        Write-Step "Found an existing $containerName container (not compose-managed)"
        Write-Host "    Leaving it untouched; migrations will run against DATABASE_URL." -ForegroundColor DarkGray
    }
} else {
    Write-Step "Starting database container"
    docker compose up -d db
    if ($LASTEXITCODE -ne 0) { Write-Fail "docker compose up failed."; exit 1 }
}

$running = Invoke-Docker ps --filter "name=^/$containerName$" --format "{{.Names}}"
if ($running -eq $containerName) {
    Write-Step "Waiting for PostgreSQL to become healthy"
    $healthy = $false
    for ($i = 0; $i -lt 24; $i++) {
        $health = Invoke-Docker inspect --format "{{.State.Health.Status}}" $containerName
        if ($LASTEXITCODE -ne 0 -or $health -eq "") { $healthy = $true; break }  # no healthcheck defined
        if ($health -eq "healthy") { $healthy = $true; break }
        Start-Sleep -Seconds 5
    }
    if ($healthy) { Write-Ok "PostgreSQL is up" }
    else { Write-Host "WARNING: container health not confirmed; continuing anyway." -ForegroundColor Yellow }
} else {
    Write-Host "WARNING: no database container running; migrations will use DATABASE_URL as configured." -ForegroundColor Yellow
}

# Compose only creates the database on first container initialisation; make
# sure the target database exists even on an adopted/pre-existing container.
$pgDb = $env:TEAMMEETINGAI_PG_DB
if (-not $pgDb) { $pgDb = "teammeetingai" }
$pgUser = $env:TEAMMEETINGAI_PG_USER
if (-not $pgUser) { $pgUser = "tma" }
$running = Invoke-Docker ps --filter "name=^/$containerName$" --format "{{.Names}}"
if ($running -eq $containerName) {
    Write-Step "Ensuring database '$pgDb' exists"
    $dbExists = Invoke-Docker exec $containerName psql -U $pgUser -d postgres -tAc "SELECT 1 FROM pg_database WHERE datname='$pgDb'"
    if ($LASTEXITCODE -eq 0 -and $dbExists -ne "1") {
        Invoke-Docker exec $containerName psql -U $pgUser -d postgres -c "CREATE DATABASE `"$pgDb`"" | Out-Null
        if ($LASTEXITCODE -ne 0) { Write-Fail "Could not create database '$pgDb'."; exit 1 }
    }
    if ($LASTEXITCODE -eq 0) { Write-Ok "Database '$pgDb' ready" }
    else { Write-Host "WARNING: could not query the container for databases; continuing." -ForegroundColor Yellow }
}

# --- dependencies and schema -------------------------------------------------

Write-Step "Installing Python dependencies (skipped if already installed)"
& $venvPython -c "import alembic, dotenv, fastapi, psycopg, sqlalchemy, uvicorn" *> $null
if ($LASTEXITCODE -ne 0) {
    & $venvPython -m pip install --upgrade pip
    & $venvPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { Write-Fail "pip install failed."; exit 1 }
}
Write-Ok "Dependencies installed"

Write-Step "Applying database migrations"
& $venvPython -m alembic upgrade head
if ($LASTEXITCODE -ne 0) {
    Write-Fail "alembic upgrade head failed. Check DATABASE_URL in .env."
    exit 1
}
Write-Ok "Schema is up to date"

# --- summary -----------------------------------------------------------------

Write-Host ""
Write-Host "Setup complete." -ForegroundColor Green
Write-Host "Next steps:"
Write-Host "  1. Start the server:   scripts\run_server.bat"
Write-Host "  2. Start the client:   python main.py  (or run.bat)"
Write-Host "  3. Daily operations:   docker compose start/stop db; backups:"
Write-Host "                         .venv\Scripts\python.exe scripts\backup_database.py"
