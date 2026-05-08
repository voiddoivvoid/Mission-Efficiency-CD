@echo off
setlocal
cd /d "%~dp0"

py -m pip install --upgrade pip
py -m pip install -r requirements.txt

py -m PyInstaller ^
  --noconsole ^
  --clean ^
  --name "MissionEff_Custom_Builder" ^
  --add-data "_internals;_internals" ^
  "MissionEff_Custom_Builder.py"

echo.
echo Done.
echo EXE is in: dist\MissionEff_Custom_Builder
echo Keep the EXE next to its _internals folder and output folder.
pause
