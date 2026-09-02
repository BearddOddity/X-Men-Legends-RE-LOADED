@echo off
REM Run the THROWAWAY reconnaissance build. Output goes to scout_stderr.txt so
REM it can never overwrite the stderr.txt the measurement tools read.
REM
REM Nothing this produces is valid progress. Do not feed it to progress.py,
REM bisect_core, or the ledger.
cd /d "D:\My Games\Xbox Recomp\src\game"
build_scout\xmen_legends_recomp.exe 1>scout_stdout.txt 2>scout_stderr.txt
set GAME_EXITCODE=%ERRORLEVEL%
echo EXITCODE=%GAME_EXITCODE%
exit /b %GAME_EXITCODE%
