@echo off
setlocal EnableExtensions
chcp 65001 >nul
rem ZCode QQ机器人 · 发布版打包脚本
rem 产物输出到仓库外目录（保持源码仓干净），默认：
rem   D:\ZCode\workspaces\qq-bot-dist\ZCodeQQBot\ZCodeQQBot.exe
rem 可用环境变量 QQBOT_DIST_ROOT 覆盖输出目录。
rem
rem 关键点（曾导致“打包后双击无反应”）：
rem   1. PyInstaller 打包时 python 环境里必须真的装有 pywebview；否则会静默
rem      产出一个缺 webview 的 exe，windowed 模式无 stdout，表现为双击无反应。
rem   2. 源码 import 的是 webview（发行包名是 pywebview），collect 的包名必须写 webview。
rem   3. 打包后执行 --self-test，确认 frozen 环境里 webview 真能导入。
rem   4. 组件缺失一律报错终止（不再 WARN+跳过）：宁可打不出包，
rem      也不能产出"能发布但缺功能/起不来"的残缺版本。

cd /d "%~dp0.."
if not defined QQBOT_DIST_ROOT set "QQBOT_DIST_ROOT=D:\ZCode\workspaces\qq-bot-dist"
set "DIST=%QQBOT_DIST_ROOT%\ZCodeQQBot"

echo [1/4] 检查打包依赖...
python -c "import sys; print(sys.executable)" 2>nul
if errorlevel 1 goto :nopython
python -c "import PyInstaller, webview, PIL" 2>nul
if errorlevel 1 goto :nodeps
python -c "import pystray" >nul 2>&1
if errorlevel 1 goto :nopystray
python -c "import edge_tts" >nul 2>&1
if errorlevel 1 goto :noedgetts

rem 用户级 site-packages 也要让 PyInstaller 看见（pip install --user 很常见）
for /f "delims=" %%i in ('python -c "import site; print(site.getusersitepackages())"') do set "PYTHONPATH=%%i;%PYTHONPATH%"

echo [2/4] 运行 PyInstaller...
python -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name "ZCodeQQBot" ^
  --icon "%CD%\plugins\qq-bot\assets\app.ico" ^
  --add-data "%CD%\plugins\qq-bot\assets;assets" ^
  --add-data "%CD%\plugins\qq-bot\scripts\plugins;plugins" ^
  --distpath "%QQBOT_DIST_ROOT%" ^
  --workpath "%QQBOT_DIST_ROOT%\build" ^
  --specpath "%QQBOT_DIST_ROOT%" ^
  --collect-all webview ^
  --hidden-import webview.platforms.winforms ^
  --hidden-import webview.platforms.edgechromium ^
  --hidden-import clr ^
  --hidden-import pystray ^
  --hidden-import PIL.Image ^
  "%CD%\plugins\qq-bot\scripts\qq_app.py"
if errorlevel 1 goto :buildfail

echo [3/4] 校验产物...
if not exist "%DIST%\ZCodeQQBot.exe" goto :verifyfail
if not exist "%DIST%\_internal\webview" goto :nowebview
if not exist "%DIST%\_internal\plugins" goto :noplugins
if not exist "%DIST%\_internal\pythonnet\runtime\Python.Runtime.dll" goto :nopythonnet
if exist "%QQBOT_DIST_ROOT%\build\ZCodeQQBot\warn-ZCodeQQBot.txt" (
  findstr /C:"missing module named webview" "%QQBOT_DIST_ROOT%\build\ZCodeQQBot\warn-ZCodeQQBot.txt" >nul 2>&1
  if not errorlevel 1 goto :nowebview
)
set "QQBOT_NO_DIALOG=1"
powershell -NoProfile -ExecutionPolicy Bypass -Command "$p = Start-Process -FilePath '%DIST%\ZCodeQQBot.exe' -ArgumentList '--self-test' -Wait -PassThru; exit $p.ExitCode"
if errorlevel 1 goto :selftestfail

echo [4/4] 完成：%DIST%\ZCodeQQBot.exe
echo.
echo 打包不含任何个人数据（Key/人设/记忆/白名单都在 ~/.zcode-qq-bot/，运行时生成）。
if /i not "%QQBOT_NO_PAUSE%"=="1" pause
exit /b 0

:nopython
echo [ERROR] 找不到可用的 python，请先安装 Python 3.10+ 并加入 PATH。
goto :fail

:nodeps
echo [ERROR] 打包依赖不完整。请先执行：
echo         python -m pip install pyinstaller pywebview pystray pillow
echo   注意：必须在运行本脚本的同一个 python 环境里安装。
goto :fail

:nopystray
echo [ERROR] 未安装 pystray：托盘是发布版的核心交互（最小化到托盘、重新打开窗口），
echo         缺了它打出的包"关闭窗口即退出"，属于残缺发布版——本脚本不再 WARN 跳过。
echo   请在同一环境执行：python -m pip install pystray 后重新打包。
goto :fail

:noedgetts
echo [ERROR] 未安装 edge-tts：语音回复插件的免费默认来源（微软公共接口）。
echo         缺了它打出的包"语音回复"无法合成（其余功能不受影响）。
echo   请在同一环境执行：python -m pip install edge-tts 后重新打包。
goto :fail

:nopythonnet
echo [ERROR] 产物缺少 pythonnet 运行时 DLL（Python.Runtime.dll），
echo   pywebview 的 WinForms 后端起不来，打出的包大概率"双击无反应"。
echo   请在同一环境执行：python -m pip install pywebview（会带 pythonnet）后重新打包。
goto :fail

:noplugins
echo [ERROR] 产物的 _internal 里没有内置插件目录（bili_scroll/weather 等），
echo   打出的包会"有界面但感知/娱乐插件全丢"。本脚本已带 --add-data 打包
echo   scripts\plugins；若仍缺失，检查仓库路径是否完整后重新打包。
goto :fail

:buildfail
echo [ERROR] PyInstaller 打包失败，请查看上面的日志。
goto :fail

:verifyfail
echo [ERROR] 产物校验失败：找不到 %DIST%\ZCodeQQBot.exe
goto :fail

:nowebview
echo [ERROR] 产物的 _internal 里没有 webview（pywebview 没有被一起打包）。
echo   常见原因：打包时的 python 环境没装 pywebview，或装在了另一个 Python 下。
echo   请在该环境执行 python -m pip install pywebview 后重新打包。
goto :fail

:selftestfail
echo [ERROR] 产物自检失败：frozen 环境无法导入 webview。
echo   详情见 %%USERPROFILE%%\.zcode-qq-bot\logs\app.log
goto :fail

:fail
if /i not "%QQBOT_NO_PAUSE%"=="1" pause
exit /b 1
