@echo off
rem Windows wrapper so an MCP client can launch the server with a single command:
rem   command = 'C:\tools\agy-mcp\agy-mcp.cmd'
rem Keep this file next to agy_mcp.py. Uses .venv\Scripts\python.exe when present,
rem otherwise whatever "python" resolves to.
setlocal
set "AGY_MCP_DIR=%~dp0"
set "AGY_MCP_PY=python"
if exist "%AGY_MCP_DIR%.venv\Scripts\python.exe" set "AGY_MCP_PY=%AGY_MCP_DIR%.venv\Scripts\python.exe"
"%AGY_MCP_PY%" "%AGY_MCP_DIR%agy_mcp.py" %*
