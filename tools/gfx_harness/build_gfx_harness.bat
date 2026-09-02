@echo off
REM Build the graphics runtime harness.
REM
REM Drives src/d3d and src/nv2a with no game attached: no guest code, no
REM recompiled functions, no static initialisers. It exists because the boot
REM cannot currently reach the graphics runtime at all - see gfx_harness.c.
REM
REM Not part of the port. Nothing it reports is game progress.
cd /d "D:\My Games\Xbox Recomp"
call "C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\VC\Auxiliary\Build\vcvars64.bat"
set CMAKE="C:\Program Files (x86)\Microsoft Visual Studio\18\BuildTools\Common7\IDE\CommonExtensions\Microsoft\CMake\CMake\bin\cmake.exe"
%CMAKE% -S tools/gfx_harness -B build_gfx_harness -G Ninja -DCMAKE_BUILD_TYPE=Release || exit /b 1
%CMAKE% --build build_gfx_harness --config Release || exit /b 1
