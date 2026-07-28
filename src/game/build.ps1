<#
.SYNOPSIS
    One-command pipeline: your ISO -> extracted files -> recompiled C -> built .exe.

.DESCRIPTION
    Mirrors the OpenGOAL / Sonic Recomp pattern: you supply your own legally-owned
    disc image, this script does everything else. Nothing copyrighted (the ISO,
    the extracted XBE/assets, or code generated from them) is ever committed to
    this repo - see .gitignore. Only non-copyrighted analysis metadata we derived
    by hand this session (tools_data/seed_functions.json - just a list of code
    addresses) is checked in, so re-running this script reproduces our fixes
    without needing to rediscover them.

.PARAMETER IsoPath
    Path to your X-Men Legends (World) Xbox disc image (.iso/.xiso).

.PARAMETER ExtractXisoExe
    Path to extract-xiso.exe. Defaults to assuming it's on PATH.
    Get it from https://github.com/XboxDev/extract-xiso if you don't have it.

.PARAMETER SkipExtract
    Skip disc extraction (use if game/ is already populated - e.g. re-running
    after only touching src/main.c or src/recomp_manual.c).

.PARAMETER SkipRecompile
    Skip the disasm/func_id/recomp pipeline (use if src/recomp/gen/ is already
    up to date - just reconfigure and rebuild).

.EXAMPLE
    .\build.ps1 -IsoPath "D:\ISOs\X-Men Legends (World).iso"

.EXAMPLE
    # Fast rebuild after editing main.c/recomp_manual.c only (no ISO needed)
    .\build.ps1 -SkipExtract -SkipRecompile
#>
param(
    [string]$IsoPath,

    [string]$ExtractXisoExe = "extract-xiso.exe",

    [switch]$SkipExtract,
    [switch]$SkipRecompile
)

$ErrorActionPreference = "Stop"

$GameDir = $PSScriptRoot
$ToolkitDir = Resolve-Path (Join-Path $GameDir "..\..")
# Extraction lands directly in game/ - this is what main.c's YOUR_GAME_DIR
# and YOUR_GAME_XBE_PATH already point at, no extra indirection needed.
$ExtractDir = Join-Path $GameDir "game"
$SeedFile = Join-Path $GameDir "tools_data\seed_functions.json"
$XbeAnalysisJson = Join-Path $GameDir "tools_data\xbe_analysis.json"

function Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }

# ── Step 1: extract the disc image ──────────────────────────────────────
if (-not $SkipExtract) {
    Step "Extracting disc image"
    if (-not (Test-Path $IsoPath)) {
        throw "ISO not found: $IsoPath"
    }
    $exe = Get-Command $ExtractXisoExe -ErrorAction SilentlyContinue
    if (-not $exe) {
        throw "extract-xiso.exe not found (looked for '$ExtractXisoExe' on PATH). " +
              "Get it from https://github.com/XboxDev/extract-xiso, or pass -ExtractXisoExe <path>."
    }
    if (Test-Path $ExtractDir) { Remove-Item $ExtractDir -Recurse -Force }
    New-Item -ItemType Directory -Path $ExtractDir | Out-Null
    & $exe -x $IsoPath -d $ExtractDir
    if ($LASTEXITCODE -ne 0) { throw "extract-xiso failed with exit code $LASTEXITCODE" }
} else {
    Step "Skipping extraction (-SkipExtract)"
    if (-not (Test-Path $ExtractDir)) {
        throw "-SkipExtract given but '$ExtractDir' doesn't exist. Run without -SkipExtract first."
    }
}

$XbePath = Join-Path $ExtractDir "default.xbe"
if (-not (Test-Path $XbePath)) {
    throw "default.xbe not found in $ExtractDir - extraction may have used a different layout."
}

# ── Step 2: recompilation pipeline ──────────────────────────────────────
if (-not $SkipRecompile) {
    New-Item -ItemType Directory -Force -Path (Split-Path $XbeAnalysisJson) | Out-Null

    Push-Location $ToolkitDir
    try {
        Step "Parsing XBE"
        py -3 -m tools.xbe_parser $XbePath --json $XbeAnalysisJson
        if ($LASTEXITCODE -ne 0) { throw "xbe_parser failed" }

        Step "Disassembling (pass 1)"
        py -3 -m tools.disasm $XbePath --text-only -v
        if ($LASTEXITCODE -ne 0) { throw "disasm (pass 1) failed" }

        if (Test-Path $SeedFile) {
            Step "Disassembling (pass 2, seeded with $((Get-Content $SeedFile | ConvertFrom-Json).Count) known addresses)"
            py -3 -m tools.disasm $XbePath --text-only -v --seed-functions $SeedFile
            if ($LASTEXITCODE -ne 0) { throw "disasm (seeded pass) failed" }
        } else {
            Write-Host ("No seed file at $SeedFile - skipping seeded pass. " +
                       "See DEBUGGING_NOTES.md for how to (re)derive one; " +
                       "expect several thousand more unresolved-stub crashes without it.") -ForegroundColor Yellow
        }

        Step "Classifying functions"
        py -3 -m tools.func_id $XbePath -v
        if ($LASTEXITCODE -ne 0) { throw "func_id failed" }

        Step "Recompiling to C"
        py -3 -m tools.recomp $XbePath --all --split 1000 --verbose
        if ($LASTEXITCODE -ne 0) { throw "recomp failed" }
    } finally {
        Pop-Location
    }

    Step "Copying generated code into project"
    $GenSrc = Join-Path $ToolkitDir "src\game\recomp\gen"
    $GenDst = Join-Path $GameDir "src\recomp\gen"
    if (-not (Test-Path $GenSrc)) {
        throw "Expected recomp output at $GenSrc - the recomp tool's default output " +
              "location may have changed; check tools/recomp/__main__.py's --gen-dir default."
    }
    if (Test-Path $GenDst) { Remove-Item "$GenDst\*" -Force }
    else { New-Item -ItemType Directory -Path $GenDst | Out-Null }
    Copy-Item "$GenSrc\*.c", "$GenSrc\*.h" -Destination $GenDst -Force
} else {
    Step "Skipping recompilation (-SkipRecompile)"
}

# ── Step 3: configure and build ─────────────────────────────────────────
Step "Configuring and building"
& (Join-Path $GameDir "build_configure.bat")
& (Join-Path $GameDir "build_compile.bat")

$ExePath = Join-Path $GameDir "build\xmen_legends_recomp.exe"
if (Test-Path $ExePath) {
    Write-Host "`nBuild succeeded: $ExePath" -ForegroundColor Green
    Write-Host "Run it with: $GameDir\run.bat" -ForegroundColor Green
} else {
    throw "Build did not produce $ExePath - check the log above."
}
