@echo off
REM Run the guest watchpoint build. Output goes to watch_stderr.txt so it never
REM overwrites the stderr.txt the measurement tools read.
REM
REM Arm it first, e.g.:
REM     set RECOMP_WATCH=0x01092B50:16
REM     run_watch.bat
REM
REM Accepts 0xVA, 0xVA+0xOFF or 0xVA:LEN (see src/kernel/xbox_watch.c).
REM
REM DIAGNOSTIC ONLY - this build changes timing. Never measure coverage or
REM progress on it.
REM
REM This exists because the executable is built for the WIN32 subsystem, so it
REM detaches from the console and PowerShell's own redirection captures
REM nothing; cmd's does. Running the exe directly from PowerShell silently
REM produces an empty log, which looks exactly like "the watchpoint never
REM fired".
cd /d "D:\My Games\Xbox Recomp\src\game"
if "%RECOMP_WATCH%"=="" (
    echo RECOMP_WATCH is not set - nothing to watch.
    exit /b 2
)
echo Watching %RECOMP_WATCH%
build_watch\xmen_legends_recomp.exe 1>watch_stdout.txt 2>watch_stderr.txt
set GAME_EXITCODE=%ERRORLEVEL%
echo EXITCODE=%GAME_EXITCODE%
exit /b %GAME_EXITCODE%
