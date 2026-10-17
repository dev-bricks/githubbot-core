@echo off
setlocal
title MULTI-BOT COMPANION
set "PYTHONIOENCODING=utf-8"
set "PYTHONUNBUFFERED=1"
echo [MULTI-BOT] Select mode and provider; the chat runs here in the foreground.
echo [MULTI-BOT] This companion stays open after the chat ends or a start fails.
REM Runtime roots and full-access opt-in come from --config or caller environment.
set "MB_PYTHON=python"
if defined MULTIBOT_PYTHON set "MB_PYTHON=%MULTIBOT_PYTHON%"
set "MB_DRY=0"
for %%A in (%*) do if /i "%%~A"=="--dry-run" set "MB_DRY=1"
"%MB_PYTHON%" -X utf8 -m githubbot_core multi-bot %*
set "MB_EXIT=%ERRORLEVEL%"
echo [MULTI-BOT] Chat ended or start aborted. Exit code: %MB_EXIT%
if not "%TASKPLAN_STARTER_DRY_RUN%"=="1" if not "%MULTIBOT_NO_PAUSE%"=="1" if not "%MB_DRY%"=="1" pause
exit /b %MB_EXIT%
