@echo off
chcp 65001 >nul
cd /d "%~dp0"
where python >nul 2>nul
if errorlevel 1 (
  echo Python が入っていないので、自動でインストールします。画面の指示に従ってください...
  winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
  echo.
  echo インストールが終わりました。このウィンドウを閉じて、もう一度このファイルをダブルクリックしてください。
  pause
  exit /b
)
python gogh_autodrop_gui.py
if errorlevel 1 pause
