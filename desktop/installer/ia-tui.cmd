@echo off
rem ia-tui - the terminal client shipped inside this installation, W-21 D5 / #817.
rem
rem It runs on the artifact's own Node runtime, resources\node\node.exe, pinned by
rem node-runtime.lock.json, because the app's own Electron binary cannot host a
rem raw-mode terminal. Measured on Electron 44.5.1: with ELECTRON_RUN_AS_NODE=1
rem process.stdin.isTTY is undefined, setRawMode does not exist, and reopening
rem fds 0 to 2 through node:tty fails with ERR_TTY_INIT_FAILED, while the same
rem console hands a real Node the same file descriptors as a TTY. VS Code's
rem bin/code.cmd shape - app exe in node mode - is therefore not enough here:
rem its CLI is line-oriented, this client is not.
rem
rem Comments above stay ASCII and free of redirection characters: cmd parses
rem pipes and angle brackets before it runs rem, so a comment mentioning angles
rem would execute its own tail.
rem
rem Usage: ia-tui.cmd --session SESSIONID
rem        ia-tui.cmd --session new
rem        ia-tui.cmd --check
rem
rem The child exit code is forwarded: W-21 #847. A bare endlocal reset it to 0,
rem so a failed TUI start read as success in scripts and in the gate readings.
setlocal
"%~dp0resources\node\node.exe" "%~dp0resources\tui\dist\src\index.js" %*
set "rc=%ERRORLEVEL%"
endlocal & exit /b %rc%
