@echo off
REM Run the RECOMP_CHECK_ABI build. Output goes to abi_stderr.txt so it never
REM overwrites the stderr.txt the normal measurement tools read.
cd /d "D:\My Games\Xbox Recomp\src\game"
REM build_abi/ uses the Visual Studio generator, so the exe lands in Release/,
REM not in the build root the way the Ninja builds do.
build_abi\Release\xmen_legends_recomp.exe 1>abi_stdout.txt 2>abi_stderr.txt
set GAME_EXITCODE=%ERRORLEVEL%
echo EXITCODE=%GAME_EXITCODE%
exit /b %GAME_EXITCODE%
