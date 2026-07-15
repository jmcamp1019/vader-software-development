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

Write-Host "[vader] Dispatching $((Get-Item $PromptFile).Length) bytes to agy..."
$sw = [System.Diagnostics.Stopwatch]::StartNew()

# Non-interactive Antigravity CLI invocation. This agy build takes the prompt
# as the -p/--print argument (not stdin) and uses --model for model selection.
# --new-project: every dispatch is a fresh session — without it agy resumes
# prior conversation state and anchors to stale draft workspaces instead of
# the prompt.
if ($Model) {
    $result = agy --new-project --model $Model --print-timeout 10m -p $prompt 2>&1
} else {
    $result = agy --new-project --print-timeout 10m -p $prompt 2>&1
}

$sw.Stop()
$result | Out-File -FilePath $OutFile -Encoding utf8

Write-Host "[vader] Done in $($sw.Elapsed.TotalSeconds.ToString('0.0'))s -> $OutFile"
Write-Host "[vader] REMINDER: output is an untrusted draft. Run the fable-gate before commit."
