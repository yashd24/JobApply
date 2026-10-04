@echo off
rem jobapply: run the job-application bot from any folder, in cmd or PowerShell.
rem Absolute paths on purpose, so this works wherever it is called from. Edit them if the project moves.
set "JOBAPPLY_HOME=D:\Projects\JobApply\resume-tailor\tailor"
set "PYTHONUTF8=1"
pushd "%JOBAPPLY_HOME%" || exit /b 1
"%JOBAPPLY_HOME%\.venv\Scripts\python.exe" "%JOBAPPLY_HOME%\jobapply.py" %*
set "JOBAPPLY_RC=%ERRORLEVEL%"
popd
exit /b %JOBAPPLY_RC%
