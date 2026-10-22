@echo off
setlocal
cd /d "%~dp0"

REM Build the GUI executable on Windows.
py -m pip install --upgrade pip pyinstaller
if errorlevel 1 (
    echo.
    echo Build failed while installing PyInstaller.
    pause
    exit /b 1
)

py -m PyInstaller --noconfirm --clean --onefile --windowed ^
  --name BattlefrontSoundExtractor ^
  --add-data "Soundtrooper01.png;." ^
  battlefront_sound_extractor.py
if errorlevel 1 (
    echo.
    echo Build failed. Please check the error messages above.
    pause
    exit /b 1
)

copy /Y "dist\BattlefrontSoundExtractor.exe" "Battlefront Sound Extractor.exe" >nul
if errorlevel 1 (
    echo.
    echo The build succeeded, but copying the EXE to the project folder failed.
    pause
    exit /b 1
)

echo.
echo Build complete!
echo.
echo Your executable is in this folder:
echo %CD%\Battlefront Sound Extractor.exe
echo.
pause
endlocal
