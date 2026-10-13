@echo off
setlocal
set "PYTHONIOENCODING=utf-8"
REM Runtime roots and full-access opt-in come from --config or caller environment.
python -m githubbot_core multi-bot %*
exit /b %ERRORLEVEL%
