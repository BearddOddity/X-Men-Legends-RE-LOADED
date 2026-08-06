<#
.SYNOPSIS
    Regression check: run the built game and verify it doesn't fall behind
    the last known-good baseline.

.DESCRIPTION
    This game doesn't boot to a menu yet, so "pass/fail" can't mean "did it
    work" - it means "did we regress below where we already got to." Runs
    run.bat (N times), parses stderr.txt, and compares against
    tools_data/smoke_baseline.json.

    FOUR signals are gated, not one. Gating kernel_calls alone is not enough:
    the nine-missing-CRT-thunks regression of 2026-08-05 cost 22 kernel calls
    AND took failed_icalls from 4 to 14. Each signal has a direction:

      kernel_calls   >= baseline   fewer calls before dying = something broke
      failed_icalls  <= baseline   MORE unresolved indirect calls = a function
                                   went missing. This is the sensitive one -
                                   it moves when seeding or discovery breaks,
                                   often before the call count does.
      heap_allocs    >= baseline   fewer allocations = the allocator or its
                                   bootstrap regressed
      safe_stub      <= baseline   more safe-stub hits = more calls landing in
                                   bodies that do nothing

    Plus the failure signatures: a hang, a stack overflow (0xC00000FD - the
    fixed CRT lock-bootstrap recursion), and one breakpoint RVA repeating far
    more than baseline (the fixed Rip+=1 corruption).

    DETERMINISM IS PART OF THE GATE. Rule #7 wants two runs per number, and a
    signal that varies between runs makes every comparison meaningless - so a
    varying signal FAILS rather than being averaged away. This matters more
    from here on: real threads introduce genuine nondeterminism, and the gate
    should say so out loud instead of flapping.

    Update the baseline (-BaselineUpdate) once you've confirmed a change is a
    genuine improvement - never lower it to make a regression pass.

.PARAMETER BaselineUpdate
    After a run that reaches further than the current baseline, call with
    this switch to record the new high-water mark.

.PARAMETER Runs
    How many times to run. Default 2 (Rule #7). Signals must agree across all
    of them.

.EXAMPLE
    .\smoke_test.ps1

.EXAMPLE
    # After confirming a fix genuinely improves things:
    .\smoke_test.ps1 -BaselineUpdate
#>
param(
    [switch]$BaselineUpdate,
    [int]$TimeoutSeconds = 30,
    [int]$Runs = 2
)

$ErrorActionPreference = "Stop"
$GameDir = $PSScriptRoot
$BaselinePath = Join-Path $GameDir "tools_data\smoke_baseline.json"
$StackOverflowExitCode = -1073741571  # 0xC00000FD, STATUS_STACK_OVERFLOW

if (-not (Test-Path (Join-Path $GameDir "build\xmen_legends_recomp.exe"))) {
    throw "No build found - run build.ps1 (or build_compile.bat) first."
}

$stderrPath = Join-Path $GameDir "stderr.txt"

# RECOMP_HANG_RIP makes the watchdog suspend the main thread and dump its RIP.
# That is a debugging aid, and it changes what this script measures: the run
# ends as a fault (or on the hard deadline) instead of a hang, so the crash
# site and the ending both move. Inherited environment is invisible, so say so
# rather than silently baselining a diagnostic build.
if ($env:RECOMP_HANG_RIP) {
    Write-Host "WARNING: RECOMP_HANG_RIP is set in this environment." -ForegroundColor Yellow
    Write-Host "  The watchdog will suspend the main thread and terminate on a" -ForegroundColor Yellow
    Write-Host "  hard deadline, so these numbers are NOT comparable to the" -ForegroundColor Yellow
    Write-Host "  baseline. Clear it before gating or recording." -ForegroundColor Yellow
}

function Invoke-OneRun {
    # A hang (infinite loop, no crash) is a real failure mode this game has
    # hit - don't let the test itself hang forever waiting for it. Launch
    # run.bat via Start-Process so we get a Process object to time out on.
    $proc = Start-Process -FilePath (Join-Path $GameDir "run.bat") -PassThru -WindowStyle Hidden
    $finished = $proc.WaitForExit($TimeoutSeconds * 1000)
    $hung = -not $finished
    if ($hung) {
        # run.bat's own child survives the batch file's termination, so kill
        # by name rather than just $proc.
        Get-Process -Name "xmen_legends_recomp" -ErrorAction SilentlyContinue | Stop-Process -Force
        Stop-Process -Id $proc.Id -Force -ErrorAction SilentlyContinue
        $exitCode = $null
    } else {
        $exitCode = $proc.ExitCode
    }

    if (-not (Test-Path $stderrPath)) {
        throw "No stderr.txt produced - run.bat may have failed to launch the exe at all."
    }
    $lines = Get-Content $stderrPath

    # kernel_calls is the HIGHEST '#n' index reached, not a line count: the
    # log numbers them, and the max survives any interleaved output.
    $km = $lines | Select-String -Pattern '\[KERNEL\] #(\d+):'
    $kernelCallCount = if ($km) {
        ($km | ForEach-Object { [int]$_.Matches[0].Groups[1].Value } | Measure-Object -Maximum).Maximum
    } else { 0 }

    $rvas = $lines | Select-String -Pattern '\[BREAKPOINT\].*RVA=0x([0-9A-Fa-f]+)' |
        ForEach-Object { $_.Matches[0].Groups[1].Value }
    $maxRepeat = 0
    if ($rvas) {
        $maxRepeat = ($rvas | Group-Object | Measure-Object -Property Count -Maximum).Maximum
    }

    # Identify the crash by RVA, never by the absolute RIP. RIP moves with
    # ASLR between process launches - the same fault has been logged as both
    # 0x7FF6B9BC716A and 0x7FF6D5FE716A - so keying on it would make the
    # determinism check below fail for no reason. RVA is image-relative and
    # stable.
    $rva = ($lines | Select-String -Pattern 'RVA=0x([0-9A-Fa-f]+)' | Select-Object -First 1)
    $crashText = if ($rva) {
        "RVA=0x" + $rva.Matches[0].Groups[1].Value
    } elseif ($lines | Select-String -Pattern '\[CRASH\]' -Quiet) {
        "(crash, no RVA logged)"
    } else { "(none)" }

    # A watchdog hang and a timeout hang are BOTH hangs, and only the second
    # was being detected. The in-process watchdog kills the game after 8 s, so
    # the process exits well inside our 30 s window and the run looked like a
    # normal termination - which meant a spinning build could be baselined as
    # known-good. Treat the watchdog's own verdict as authoritative.
    $watchdogHang = [bool]($lines | Select-String -Pattern '\[WATCHDOG\] No progress' -Quiet)

    [pscustomobject]@{
        Hung         = ($hung -or $watchdogHang)
        TimedOut     = $hung
        WatchdogHang = $watchdogHang
        ExitCode     = $exitCode
        KernelCalls  = $kernelCallCount
        FailedIcalls = ($lines | Select-String -Pattern 'Failed to resolve VA').Count
        HeapAllocs   = ($lines | Select-String -Pattern '\[HEAP\] #').Count
        SafeStub     = ($lines | Select-String -Pattern '\[SAFE_STUB\]').Count
        MaxRepeat    = $maxRepeat
        Recursion    = ($lines | Select-String -Pattern '\[RECURSION\]').Count
        CrashSite    = $crashText
    }
}

$results = @()
for ($i = 1; $i -le $Runs; $i++) {
    Write-Host "Run $i/$Runs (timeout ${TimeoutSeconds}s)..." -ForegroundColor Cyan
    $r = Invoke-OneRun
    $results += $r
    Write-Host ("  kernel_calls={0}  failed_icalls={1}  heap_allocs={2}  safe_stub={3}  {4}" -f `
        $r.KernelCalls, $r.FailedIcalls, $r.HeapAllocs, $r.SafeStub,
        $(if ($r.Hung) { "HUNG" } else { $r.CrashSite }))
}

# -- Determinism ----------------------------------------------------------
# A signal that moves between runs invalidates every comparison made with it,
# so this fails rather than averaging. Reported before the baseline check
# because "it varies" explains any baseline result that follows.
$varying = @()
foreach ($name in @("KernelCalls", "FailedIcalls", "HeapAllocs", "SafeStub", "CrashSite")) {
    $distinct = @($results | ForEach-Object { $_.$name } | Sort-Object -Unique)
    if ($distinct.Count -gt 1) {
        $varying += ("{0} ({1})" -f $name, ($distinct -join ", "))
    }
}

# A HUNG run is time-boxed by the watchdog, so every counter is "however far it
# got in 8 seconds" and will differ run to run by construction. That is not the
# nondeterminism this check exists to catch - real nondeterminism is a build
# whose BEHAVIOUR varies - and reporting it as such trains people to ignore the
# warning. Observed: safe_stub 8646/8910/9146 across three runs of one spin,
# which decodes to ~600M identical stub calls, not three different executions.
# The hang itself still fails below; this only stops the double-report.
$anyHungForVariance = [bool]($results | Where-Object { $_.Hung })
if ($anyHungForVariance -and $varying.Count -gt 0) {
    Write-Host ""
    Write-Host "NOTE: the run HANGS, so counters are time-boxed by the watchdog" -ForegroundColor Cyan
    Write-Host "      and differ by how far each run got. Not treated as" -ForegroundColor Cyan
    Write-Host "      nondeterminism; the hang is reported on its own below." -ForegroundColor Cyan
    $varying | ForEach-Object { Write-Host "        time-boxed: $_" -ForegroundColor Cyan }
    $varying = @()
}

# Worst case per signal, so the gate judges the least favourable run rather
# than a lucky one.
$kernelCalls  = ($results | Measure-Object -Property KernelCalls  -Minimum).Minimum
$failedIcalls = ($results | Measure-Object -Property FailedIcalls -Maximum).Maximum
$heapAllocs   = ($results | Measure-Object -Property HeapAllocs   -Minimum).Minimum
$safeStub     = ($results | Measure-Object -Property SafeStub     -Maximum).Maximum
$maxRepeat    = ($results | Measure-Object -Property MaxRepeat    -Maximum).Maximum
$anyHung      = [bool]($results | Where-Object { $_.Hung })
$anyStackOver = [bool]($results | Where-Object { $_.ExitCode -eq $StackOverflowExitCode })

Write-Host ""
Write-Host ("worst-case over {0} run(s): kernel_calls={1}  failed_icalls={2}  heap_allocs={3}  safe_stub={4}" -f `
    $Runs, $kernelCalls, $failedIcalls, $heapAllocs, $safeStub)

# -- Update baseline and exit ---------------------------------------------
if ($BaselineUpdate) {
    if ($anyHung) {
        Write-Host "`nRefusing to record a baseline from a hung run - that's not a known-good state." -ForegroundColor Red
        exit 1
    }
    if ($varying.Count -gt 0) {
        Write-Host "`nRefusing to record a baseline from a NON-DETERMINISTIC build - the numbers would not mean anything:" -ForegroundColor Red
        $varying | ForEach-Object { Write-Host "  varies: $_" -ForegroundColor Red }
        exit 1
    }
    $baseline = [ordered]@{
        min_kernel_calls       = $kernelCalls
        max_failed_icalls      = $failedIcalls
        min_heap_allocs        = $heapAllocs
        max_safe_stub          = $safeStub
        max_breakpoint_repeats = [Math]::Max($maxRepeat, 5)
        crash_site             = $results[0].CrashSite
        runs                   = $Runs
        recorded_utc           = (Get-Date).ToUniversalTime().ToString("o")
        note                   = "Update only after confirming this run genuinely reaches further than before - never lower these to make a regression pass. failed_icalls is the sensitive one: it rises when a function goes missing, often before the call count moves."
    }
    $baseline | ConvertTo-Json | Set-Content $BaselinePath
    Write-Host ("`nBaseline updated: {0}/{1}/{2}/{3}" -f $kernelCalls, $failedIcalls, $heapAllocs, $safeStub) -ForegroundColor Green
    exit 0
}

if (-not (Test-Path $BaselinePath)) {
    Write-Host "`nNo baseline yet at $BaselinePath - run with -BaselineUpdate to record one." -ForegroundColor Yellow
    exit 0
}

$baseline = Get-Content $BaselinePath | ConvertFrom-Json
$failed = $false

if ($varying.Count -gt 0) {
    Write-Host "`nFAIL: NON-DETERMINISTIC - a signal moved between runs, so no comparison below is evidence." -ForegroundColor Red
    $varying | ForEach-Object { Write-Host "  varies: $_" -ForegroundColor Red }
    $failed = $true
}

if ($anyHung) {
    $why = if ($results | Where-Object { $_.WatchdogHang }) {
        "the in-process watchdog fired (spin detected, process self-terminated)"
    } else {
        "exceeded ${TimeoutSeconds}s with no exit and was killed"
    }
    Write-Host "`nFAIL: process hung - $why. A hang is not a known-good state." -ForegroundColor Red
    $failed = $true
}

if ($anyStackOver) {
    Write-Host "`nFAIL: stack overflow (0xC00000FD) - matches the signature of the fixed CRT lock-bootstrap recursion bug. Did something reintroduce it?" -ForegroundColor Red
    $failed = $true
}

if ($kernelCalls -lt $baseline.min_kernel_calls) {
    Write-Host "`nFAIL: only reached $kernelCalls kernel calls, baseline is $($baseline.min_kernel_calls). Something that used to work broke." -ForegroundColor Red
    $failed = $true
}

# Guarded with -ne $null so an older baseline file lacking these keys still
# runs the checks it does define, instead of comparing against nothing.
if ($null -ne $baseline.max_failed_icalls -and $failedIcalls -gt $baseline.max_failed_icalls) {
    Write-Host "`nFAIL: $failedIcalls failed indirect calls, baseline allows $($baseline.max_failed_icalls). A rise here means a FUNCTION WENT MISSING - check seed_list.json before suspecting the lifter." -ForegroundColor Red
    $failed = $true
}

if ($null -ne $baseline.min_heap_allocs -and $heapAllocs -lt $baseline.min_heap_allocs) {
    Write-Host "`nFAIL: only $heapAllocs heap allocations, baseline is $($baseline.min_heap_allocs) - the allocator or its bootstrap regressed." -ForegroundColor Red
    $failed = $true
}

if ($null -ne $baseline.max_safe_stub -and $safeStub -gt $baseline.max_safe_stub) {
    Write-Host "`nFAIL: $safeStub safe-stub hits, baseline allows $($baseline.max_safe_stub) - more calls are landing in bodies that do nothing." -ForegroundColor Red
    $failed = $true
}

if ($maxRepeat -gt $baseline.max_breakpoint_repeats) {
    Write-Host "`nFAIL: a single breakpoint RVA repeated $maxRepeat times (baseline allows $($baseline.max_breakpoint_repeats)) - matches the signature of the fixed Rip+=1 corruption bug." -ForegroundColor Red
    $failed = $true
}

if ($failed) {
    Write-Host "`nSee $stderrPath for details." -ForegroundColor Red
    exit 1
}

Write-Host ("`nOK - no regression vs. baseline ({0}/{1}/{2}/{3})." -f `
    $baseline.min_kernel_calls, $baseline.max_failed_icalls,
    $baseline.min_heap_allocs, $baseline.max_safe_stub) -ForegroundColor Green

# A moved crash site is not a failure - going further SHOULD move it - but it
# is always worth knowing, because it is the cheapest signal that behaviour
# changed even when every count held.
if ($null -ne $baseline.crash_site -and $results[0].CrashSite -ne $baseline.crash_site) {
    Write-Host "NOTE: crash site moved." -ForegroundColor Cyan
    Write-Host "  baseline: $($baseline.crash_site)" -ForegroundColor Cyan
    Write-Host "  now     : $($results[0].CrashSite)" -ForegroundColor Cyan
}
if ($kernelCalls -gt $baseline.min_kernel_calls) {
    Write-Host "This run reached FURTHER than baseline ($kernelCalls > $($baseline.min_kernel_calls)) - consider -BaselineUpdate." -ForegroundColor Cyan
}
exit 0
