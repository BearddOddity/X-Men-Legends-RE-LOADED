<#
.SYNOPSIS
    Regression check: run the built game and verify it doesn't fall behind
    the last known-good baseline.

.DESCRIPTION
    This game doesn't boot to a menu yet, so "pass/fail" can't mean "did it
    work" - it means "did we regress below where we already got to." Runs
    run.bat, parses stderr.txt, and compares against
    tools_data/smoke_baseline.json:
      - kernel call count must reach at least the baseline (fewer calls
        before crashing means something that used to work broke)
      - exit code must not be a signature we've already fixed and know is
        bad (stack overflow from the old lock-recursion bug, for instance)
      - no single native RVA should re-trigger a huge number of times (the
        signature of the breakpoint-handler corruption bug from this
        session - a healthy run has each address logged only a handful of
        times, not thousands)

    Update the baseline (BaselineUpdate) once you've confirmed a change is
    a genuine improvement (reaches further than before) - never lower it to
    make a regression pass.

.PARAMETER BaselineUpdate
    After a run that reaches further than the current baseline, call with
    this switch to record the new high-water mark.

.EXAMPLE
    .\smoke_test.ps1

.EXAMPLE
    # After confirming a fix genuinely improves things:
    .\smoke_test.ps1 -BaselineUpdate
#>
param(
    [switch]$BaselineUpdate,
    [int]$TimeoutSeconds = 30
)

$ErrorActionPreference = "Stop"
$GameDir = $PSScriptRoot
$BaselinePath = Join-Path $GameDir "tools_data\smoke_baseline.json"
$StackOverflowExitCode = -1073741571  # 0xC00000FD, STATUS_STACK_OVERFLOW
$HungExitCode = -1  # sentinel: process was killed for exceeding $TimeoutSeconds

if (-not (Test-Path (Join-Path $GameDir "build\xmen_legends_recomp.exe"))) {
    throw "No build found - run build.ps1 (or build_compile.bat) first."
}

# A hang (infinite loop, no crash) is a real failure mode this game has hit -
# don't let the test itself hang forever waiting for it. Launch run.bat
# directly (not via `&`) so we get a Process object to enforce a timeout on.
Write-Host "Running (timeout ${TimeoutSeconds}s)..." -ForegroundColor Cyan
$proc = Start-Process -FilePath (Join-Path $GameDir "run.bat") -PassThru -WindowStyle Hidden
$finished = $proc.WaitForExit($TimeoutSeconds * 1000)
$hung = -not $finished
if ($hung) {
    Write-Host "  Process exceeded ${TimeoutSeconds}s with no exit - treating as a hang, killing it." -ForegroundColor Yellow
    # run.bat's own child (the game exe) survives the batch file's own
    # termination, so kill by name rather than just $proc.
    Get-Process -Name "xmen_legends_recomp" -ErrorAction SilentlyContinue | Stop-Process -Force
    Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
    $exitCode = $HungExitCode
} else {
    $exitCode = $proc.ExitCode
}

$stderrPath = Join-Path $GameDir "stderr.txt"
if (-not (Test-Path $stderrPath)) {
    throw "No stderr.txt produced - run.bat may have failed to launch the exe at all."
}
$lines = Get-Content $stderrPath

# ── Gather metrics ───────────────────────────────────────────────────────
$kernelCallMatches = $lines | Select-String -Pattern '\[KERNEL\] #(\d+):'
$kernelCallCount = if ($kernelCallMatches) {
    ($kernelCallMatches | ForEach-Object { [int]$_.Matches[0].Groups[1].Value } | Measure-Object -Maximum).Maximum
} else { 0 }

$breakpointRvas = $lines | Select-String -Pattern '\[BREAKPOINT\].*RVA=0x([0-9A-Fa-f]+)' |
    ForEach-Object { $_.Matches[0].Groups[1].Value }
$maxRepeatedBreakpoint = 0
if ($breakpointRvas) {
    $maxRepeatedBreakpoint = ($breakpointRvas | Group-Object | Measure-Object -Property Count -Maximum).Maximum
}

$recursionMarkers = ($lines | Select-String -Pattern '\[RECURSION\]').Count

Write-Host "  Exit code: $exitCode$(if ($hung) { ' (killed - hung)' })"
Write-Host "  Kernel calls reached: $kernelCallCount"
Write-Host "  Max repeats of a single breakpoint RVA: $maxRepeatedBreakpoint"
Write-Host "  Leftover [RECURSION] diagnostic lines: $recursionMarkers"

# ── Update baseline and exit ─────────────────────────────────────────────
if ($BaselineUpdate) {
    if ($hung) {
        Write-Host "`nRefusing to record a baseline from a hung run - that's not a known-good state." -ForegroundColor Red
        exit 1
    }
    $baseline = @{
        min_kernel_calls        = $kernelCallCount
        max_breakpoint_repeats  = [Math]::Max($maxRepeatedBreakpoint, 5)
        recorded_utc            = (Get-Date).ToUniversalTime().ToString("o")
        note                    = "Update only after confirming this run genuinely reaches further than before - never lower these to make a regression pass."
    }
    $baseline | ConvertTo-Json | Set-Content $BaselinePath
    Write-Host "`nBaseline updated: kernel_calls=$kernelCallCount" -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $BaselinePath)) {
    Write-Host "`nNo baseline yet at $BaselinePath - run with -BaselineUpdate to record one." -ForegroundColor Yellow
    exit 0
}

$baseline = Get-Content $BaselinePath | ConvertFrom-Json
$failed = $false

if ($hung) {
    Write-Host "`nFAIL: process hung (exceeded ${TimeoutSeconds}s with no exit) and was killed - a real regression, not a crash." -ForegroundColor Red
    $failed = $true
}

if ($exitCode -eq $StackOverflowExitCode) {
    Write-Host "`nFAIL: stack overflow (0xC00000FD) - matches the signature of the fixed CRT lock-bootstrap recursion bug. Did something reintroduce it?" -ForegroundColor Red
    $failed = $true
}

if ($kernelCallCount -lt $baseline.min_kernel_calls) {
    Write-Host "`nFAIL: only reached $kernelCallCount kernel calls, baseline is $($baseline.min_kernel_calls). Something that used to work broke." -ForegroundColor Red
    $failed = $true
}

if ($maxRepeatedBreakpoint -gt $baseline.max_breakpoint_repeats) {
    Write-Host "`nFAIL: a single breakpoint RVA repeated $maxRepeatedBreakpoint times (baseline allows $($baseline.max_breakpoint_repeats)) - matches the signature of the fixed Rip+=1 corruption bug." -ForegroundColor Red
    $failed = $true
}

if ($failed) {
    Write-Host "`nSee $stderrPath for details." -ForegroundColor Red
    exit 1
}

Write-Host "`nOK - no regression vs. baseline ($($baseline.min_kernel_calls) kernel calls)." -ForegroundColor Green
if ($kernelCallCount -gt $baseline.min_kernel_calls) {
    Write-Host "This run reached FURTHER than baseline ($kernelCallCount > $($baseline.min_kernel_calls)) - consider -BaselineUpdate." -ForegroundColor Cyan
}
exit 0
