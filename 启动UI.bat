@echo off
setlocal
title Snakeer Drawing
cd /d "%~dp0"

echo ================================================================
echo    Snakeer Drawing
echo ================================================================
echo.

rem --- 找 Python ---------------------------------------------------
rem  不写死解释器路径：每台机器装的位置都不一样。
rem
rem  ★ 为什么不能只用 where python：
rem    Windows 10/11 自带一个"微软商店占位程序"，住在 WindowsApps 目录下
rem    （用户目录\AppData\Local\Microsoft\WindowsApps\python.exe）。
rem    它会被 where python 找到，但它**什么都不干**。只用 where 判断
rem    "装没装 Python"，在这类机器上会得到"找到了"，然后静默失败、
rem    窗口一闪而过，用户完全看不出原因。
rem
rem    所以这里改成**试跑**：让候选解释器真的打印约定值 123，打出来才算数。
rem    版本要求（3.10+）也在同一条命令里一起验了。
set "PY="
for %%C in (python py) do (
  if not defined PY (
    for /f "delims=" %%O in ('%%C -c "import sys;print(123 if sys.version_info>=(3,10) else 0)" 2^>nul') do (
      if "%%O"=="123" set "PY=%%C"
    )
  )
)
if not defined PY (
  echo   没有找到可用的 Python（需要 3.10 或更高版本）。
  echo.
  echo   到 https://www.python.org/downloads/ 装一个 Python 3.12，
  echo   安装时勾选 "Add Python to PATH"，装完重新双击本文件。
  echo.
  echo   如果明明装了却还提示这个：可能是从微软商店装的版本，
  echo   那种不能用，请换成 python.org 的安装包版。
  echo.
  pause
  exit /b 1
)

rem --- 找服务器脚本 ------------------------------------------------
rem  %~dp0 = 本 bat 所在目录。用相对位置，不写死盘符：
rem  换台电脑、换个目录都不用改这个文件。
set "SRV=%~dp0server.py"
if not exist "%SRV%" (
  echo   找不到 server.py。
  echo   它应该和本文件在同一个目录：
  echo       %~dp0
  echo   请确认解压之后没有把文件单独挪出来过。
  echo.
  pause
  exit /b 1
)

rem --- 端口 --------------------------------------------------------
rem  这里故意不管端口：server.py 会读 config.json 的 port，没有就从 8765
rem  开始往上找第一个空闲端口。所以在 bat 里判断"端口被占"是重复且会过时的。
echo   正在启动... 浏览器会自动打开页面，地址以浏览器地址栏为准。
echo.
echo   注意: ComfyUI 必须先开着。关掉本窗口 = 停止服务。
echo ================================================================
echo.

%PY% -X utf8 "%SRV%"

echo.
echo ================================================================
echo    服务已停止（本窗口没有关闭）。
echo ================================================================
pause >nul
endlocal
