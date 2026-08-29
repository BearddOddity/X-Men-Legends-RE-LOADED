@echo off
REM Run the guest page-zero census build. Output goes to page0_stderr.txt so it
REM never overwrites the stderr.txt the normal measurement tools read.
cd /d "D:\My Games\Xbox Recomp\src\game"
build_page0\xmen_legends_recomp.exe 1>page0_stdout.txt 2>page0_stderr.txt
set GAME_EXITCODE=%ERRORLEVEL%
echo EXITCODE=%GAME_EXITCODE%
exit /b %GAME_EXITCODE%
