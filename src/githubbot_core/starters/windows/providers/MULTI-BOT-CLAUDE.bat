@echo off
setlocal
REM Delegate githubbot_core multi-bot through the neutral foreground companion.
call "%~dp0..\MULTI-BOT.bat" --provider claude --no-interactive %*
exit /b %ERRORLEVEL%
