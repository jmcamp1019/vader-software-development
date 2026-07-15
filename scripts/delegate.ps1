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

# Non-interactive Antigravity CLI invocation. If your agy version uses different
# flags for one-shot prompts or model selection, adjust this line (check: agy --help).
if ($Model) {
    $result = $prompt | agy -m $Model -p 2>&1
} else {
    $result = $prompt | agy -p 2>&1
}

$sw.Stop()
$result | Out-File -FilePath $OutFile -Encoding utf8

Write-Host "[vader] Done in $($sw.Elapsed.TotalSeconds.ToString('0.0'))s -> $OutFile"
Write-Host "[vader] REMINDER: output is an untrusted draft. Run the fable-gate before commit."
