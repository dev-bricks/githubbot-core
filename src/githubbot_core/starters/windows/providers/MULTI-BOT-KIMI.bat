@echo off
setlocal
python -m githubbot_core multi-bot --provider kimi --no-interactive %*
exit /b %ERRORLEVEL%
