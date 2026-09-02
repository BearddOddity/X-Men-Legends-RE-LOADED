@echo off
REM Configure and build the THROWAWAY reconnaissance build.
REM
REM Each of the ten static initialisers is wrapped in SEH, so a fault skips
REM that initialiser and the boot continues to the next one - the same way
REM build_page0 censuses page zero in a single pass instead of dying at the
REM first hit. One run yields every failing initialiser, and then shows how
REM far execution gets once they are all behind it.
REM
REM It exists to answer one question: does the D3D/NV2A host code produce
REM anything? That code sits downstream of all ten initialisers and has never
REM executed.
REM
REM THROWAWAY. It deliberately continues past faults the real port must not
REM continue past. NEVER measure coverage or progress on this build, and never
REM record a run of it in progress.json or the ledger. Use build/ for that.
REM
REM Output goes to build_scout/, separate from build/, the same way
REM build_page0/ works for RECOMP_TRAP_PAGE_ZERO.
cd /d "D:\My Games\Xbox Recomp\src\game"
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set CMAKE="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
%CMAKE% -S . -B build_scout -G Ninja -DCMAKE_BUILD_TYPE=Release -DRECOMP_SCOUT=ON || exit /b 1
%CMAKE% --build build_scout --config Release || exit /b 1
