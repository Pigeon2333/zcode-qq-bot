@echo off
rem ZCode QQ机器人 · 发布版打包脚本
rem 产物输出到仓库外目录（保持源码仓干净）：
rem   D:\ZCode\workspaces\qq-bot-dist\ZCodeQQBot\ZCodeQQBot.exe
rem 前置：pip install pyinstaller pywebview pystray pillow

cd /d "%~dp0.."
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name "ZCodeQQBot" ^
  --icon "%CD%\plugins\qq-bot\assets\app.ico" ^
  --add-data "%CD%\plugins\qq-bot\assets;assets" ^
  --distpath "D:\ZCode\workspaces\qq-bot-dist" ^
  --workpath "D:\ZCode\workspaces\qq-bot-dist\build" ^
  --specpath "D:\ZCode\workspaces\qq-bot-dist" ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import webview.platforms.edgechromium ^
  --collect-submodules pywebview ^
  plugins\qq-bot\scripts\qq_app.py

echo.
echo 完成：D:\ZCode\workspaces\qq-bot-dist\ZCodeQQBot\ZCodeQQBot.exe
echo 打包不包含任何个人数据（Key/人设/记忆/白名单都在 ~/.zcode-qq-bot/，运行时生成）
pause
