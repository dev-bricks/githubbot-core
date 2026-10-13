@echo off
setlocal
python -m githubbot_core multi-bot --provider claude --no-interactive %*
exit /b %ERRORLEVEL%
