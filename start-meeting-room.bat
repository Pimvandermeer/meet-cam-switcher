@echo off
rem Meeting room: OBS virtual camera + automatic presenter/audience camera switching.
rem Put a shortcut to this file in shell:startup. Set the OBS WebSocket password once: setx OBS_PASSWORD "..."
set "OBS_DIR=C:\Program Files\obs-studio\bin\64bit"
rem OBS must start from its own folder or it can't find its data files.
tasklist /fi "imagename eq obs64.exe" | find /i "obs64.exe" >nul || start "" /d "%OBS_DIR%" obs64.exe --startvirtualcam --minimize-to-tray --disable-shutdown-check
cd /d "%~dp0"
uv run meet_cam_switcher.py %*
pause
