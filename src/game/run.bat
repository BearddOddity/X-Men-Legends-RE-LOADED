@echo off
cd /d "D:\My Games\Xbox Recomp\src\game"
build\xmen_legends_recomp.exe 1>stdout.txt 2>stderr.txt
echo EXITCODE=%ERRORLEVEL%
