# scripts/delegate.ps1 - Vader dispatch wrapper for the Antigravity CLI (agy)
# Usage: powershell -File scripts/delegate.ps1 -PromptFile scratch/task.md -OutFile scratch/out.md
param(
    [Parameter(Mandatory = $true)][string]$PromptFile,
    [Parameter(Mandatory = $true)][string]$OutFile,
    [string]$Model = ""
)

$ErrorActionPreference = "Stop"

if (-not (Test-Path $PromptFile)) {
    Write-Error "Prompt file not found: $PromptFile"
    exit 1
}

$prompt = Get-Content -Path $PromptFile -Raw
$outDir = Split-Path -Parent $OutFile
if ($outDir -and -not (Test-Path $outDir)) { New-Item -ItemType Directory -Path $outDir | Out-Null }
$outFileAbs = Join-Path (Get-Location) $OutFile

Write-Host "[vader] Dispatching $((Get-Item $PromptFile).Length) bytes to agy..."
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# Non-interactive Antigravity CLI invocation. This agy build takes the prompt
# as the -p/--print argument (not stdin) and uses --model for model selection.
# --new-project: every dispatch is a fresh session — without it agy resumes
# prior conversation state and anchors to stale draft workspaces instead of
# the prompt.
# Isolation (post-incident): agy runs from a fresh empty temp directory with
# --sandbox, never from the repo — a prior dispatch run from the repo cwd
# wrote into the repo and created unauthorized commits. Only the printed
# draft captured in -OutFile is ever reviewed.
$isolation = Join-Path ([System.IO.Path]::GetTempPath()) ("vader-dispatch-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $isolation | Out-Null
Push-Location $isolation
try {
    if ($Model) {
        $result = agy --sandbox --new-project --model $Model --print-timeout 10m -p $prompt 2>&1
    } else {
        $result = agy --sandbox --new-project --print-timeout 10m -p $prompt 2>&1
    }
} finally {
    Pop-Location
}

$sw.Stop()
$result | Out-File -FilePath $outFileAbs -Encoding utf8

Write-Host "[vader] Done in $($sw.Elapsed.TotalSeconds.ToString('0.0'))s -> $OutFile"
Write-Host "[vader] REMINDER: output is an untrusted draft. Run the fable-gate before commit."
