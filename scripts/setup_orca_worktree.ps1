param()

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

function Invoke-SetupCommand {
    param([string]$Command, [string[]]$Arguments)

    $global:LASTEXITCODE = 0
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) {
        throw "$Command failed with exit code $LASTEXITCODE."
    }
}

foreach ($variableName in @('ORCA_ROOT_PATH', 'ORCA_WORKTREE_PATH')) {
    $value = [Environment]::GetEnvironmentVariable($variableName)
    if ([string]::IsNullOrWhiteSpace($value)) {
        throw "Missing Orca environment variable: $variableName."
    }
    if (-not [IO.Path]::IsPathRooted($value)) {
        throw "$variableName must be an absolute directory path."
    }
    if (-not (Test-Path -LiteralPath $value -PathType Container)) {
        throw "$variableName must point to an existing directory."
    }
}

$rootPath = (Resolve-Path -LiteralPath $env:ORCA_ROOT_PATH).ProviderPath
$worktreePath = (Resolve-Path -LiteralPath $env:ORCA_WORKTREE_PATH).ProviderPath
if ($rootPath.TrimEnd('\', '/') -eq $worktreePath.TrimEnd('\', '/')) {
    throw 'Setup requires a new worktree, separate from the root checkout.'
}

foreach ($relativePath in @('pyproject.toml', 'uv.lock', 'frontend/package.json')) {
    if (-not (Test-Path -LiteralPath (Join-Path $worktreePath $relativePath) -PathType Leaf)) {
        throw "Incomplete worktree: missing $relativePath."
    }
}

$uvCommand = (Get-Command uv -ErrorAction Stop).Name
$npmCommand = (Get-Command npm.cmd -ErrorAction Stop).Name
$null = Get-Command node -ErrorAction Stop

Push-Location -LiteralPath $worktreePath
$previousUvEnvironment = $env:UV_PROJECT_ENVIRONMENT
try {
    $targetEnv = Join-Path $worktreePath '.env'
    if (-not (Test-Path -LiteralPath $targetEnv)) {
        $sourceEnv = Join-Path $rootPath '.env'
        if (-not (Test-Path -LiteralPath $sourceEnv -PathType Leaf)) {
            $sourceEnv = Join-Path $worktreePath '.env.example'
        }
        if (-not (Test-Path -LiteralPath $sourceEnv -PathType Leaf)) {
            throw 'Neither the root .env nor the worktree .env.example exists.'
        }
        # CreateNew preserves an existing configuration even if another setup races us.
        $sourceStream = [IO.File]::OpenRead($sourceEnv)
        try {
            $targetStream = [IO.File]::Open($targetEnv, [IO.FileMode]::CreateNew)
            try {
                $sourceStream.CopyTo($targetStream)
            }
            finally {
                $targetStream.Dispose()
            }
        }
        finally {
            $sourceStream.Dispose()
        }
        Write-Host '[setup] Created local .env; review its service and database targets before use.'
    }
    else {
        Write-Host '[setup] Preserved existing .env.'
    }

    $env:UV_PROJECT_ENVIRONMENT = Join-Path $worktreePath '.venv'
    Write-Host '[setup] Installing locked Python dependencies into the worktree .venv.'
    Invoke-SetupCommand -Command $uvCommand -Arguments @('sync', '--frozen')

    $frontendPath = Join-Path $worktreePath 'frontend'
    Push-Location -LiteralPath $frontendPath
    try {
        if (Test-Path -LiteralPath (Join-Path $frontendPath 'package-lock.json') -PathType Leaf) {
            Write-Host '[setup] Installing locked frontend dependencies.'
            Invoke-SetupCommand -Command $npmCommand -Arguments @('ci', '--no-audit', '--no-fund')
        }
        else {
            Write-Host '[setup] No frontend lockfile; npm install will resolve package.json ranges.'
            Invoke-SetupCommand -Command $npmCommand -Arguments @('install', '--no-audit', '--no-fund')
        }
    }
    finally {
        Pop-Location
    }

    Write-Host '[setup] Dependency setup complete. Database migrations and application startup are separate steps.'
}
finally {
    $env:UV_PROJECT_ENVIRONMENT = $previousUvEnvironment
    Pop-Location
}
