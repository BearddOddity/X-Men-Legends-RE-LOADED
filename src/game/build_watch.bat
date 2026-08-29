@echo off
REM Configure and build the guest guest watchpoint build.
REM
REM Guest VA 0x0-0xFFF becomes PAGE_NOACCESS and every access to it is logged
REM with its guest offset and faulting RVA, then resumed - so one boot yields
REM the whole census instead of dying at the first hit.
REM
REM DIAGNOSTIC ONLY. It changes timing. Never measure coverage or progress on
REM this build; use the normal build/ for that.
REM
REM Output goes to build_watch/, separate from build/, the same way
REM build_abi/ works for RECOMP_CHECK_ABI.
cd /d "D:\My Games\Xbox Recomp\src\game"
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set CMAKE="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
%CMAKE% -S . -B build_watch -G Ninja -DCMAKE_BUILD_TYPE=Release -DRECOMP_WATCH_GUEST=ON || exit /b 1
%CMAKE% --build build_watch --config Release || exit /b 1
