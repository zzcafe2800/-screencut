@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "BRANCH=claude/gallant-heisenberg-4vjhkx"
set "URL=https://raw.githubusercontent.com/zzcafe2800/-screencut/%BRANCH%/tools/gogh_autodrop_gui.py"
if exist no_update.txt (echo 自動アップデート: オフ) else echo 最新版を確認しています...
if not exist no_update.txt powershell -NoProfile -Command "try { Invoke-WebRequest -UseBasicParsing -Uri '%URL%' -OutFile gogh_new.tmp -TimeoutSec 10; if ((Get-Item gogh_new.tmp).Length -gt 5000 -and (Select-String -Path gogh_new.tmp -Pattern 'gogh' -Quiet)) { Move-Item -Force gogh_new.tmp gogh_autodrop_gui.py; '最新版にしました' } else { Remove-Item gogh_new.tmp; '更新なし' } } catch { '通信できないため、今ある版で起動します' }"
where python >nul 2>nul
if errorlevel 1 goto nopy
python gogh_autodrop_gui.py
if errorlevel 1 pause
exit /b
:nopy
echo Python が入っていないので、自動でインストールします。画面の指示に従ってください...
winget install -e --id Python.Python.3.12 --accept-source-agreements --accept-package-agreements
echo.
echo インストールが終わりました。このウィンドウを閉じて、もう一度このファイルをダブルクリックしてください。
pause
