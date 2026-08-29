@echo off
REM Configure and build the callee-save / esp checking build (RECOMP_CHECK_ABI).
REM
REM Verifies at every call that the callee gave back ebx/esi/edi and left esp
REM where it found it. Diagnostic only - it is behaviour-neutral but slow, so
REM never measure coverage or progress on it.
REM
REM Output goes to build_abi/, separate from build/, the same way build_page0/
REM works for RECOMP_TRAP_PAGE_ZERO.
cd /d "D:\My Games\Xbox Recomp\src\game"
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set CMAKE="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
REM No -G here on purpose: build_abi/ was originally configured with the
REM "Visual Studio 18 2026" generator, and passing a different one makes CMake
REM refuse rather than reconfigure. Omitting it reuses whatever the cache has.
%CMAKE% -S . -B build_abi -DCMAKE_BUILD_TYPE=Release -DRECOMP_CHECK_ABI=ON || exit /b 1
%CMAKE% --build build_abi --config Release || exit /b 1
