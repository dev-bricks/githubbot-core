@echo off
setlocal
python -m githubbot_core multi-bot --provider codex --no-interactive %*
exit /b %ERRORLEVEL%
