@echo off
cd /d "D:\My Games\Xbox Recomp\src\game"
build\xmen_legends_recomp.exe 1>stdout.txt 2>stderr.txt
set GAME_EXITCODE=%ERRORLEVEL%
echo EXITCODE=%GAME_EXITCODE%
exit /b %GAME_EXITCODE%
