@echo off
setlocal
python -m githubbot_core multi-bot --provider agy --no-interactive %*
exit /b %ERRORLEVEL%
