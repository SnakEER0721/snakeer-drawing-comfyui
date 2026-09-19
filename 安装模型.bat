@echo off
setlocal
title Snakeer Drawing - 安装模型
cd /d "%~dp0"

echo ================================================================
echo    Snakeer Drawing —— 安装刚需模型
echo ================================================================
echo.
echo    把从网盘下载的模型文件，全部放进这个文件夹：
echo.
echo        %~dp0刚需模型全部放这
echo.
echo    然后双击本文件。脚本会自动把它们放到 ComfyUI 的对应目录，
echo    并把 config.json 的底模改成实际装进去的那个。
echo.
echo    也可以把文件或文件夹**直接拖到本文件上**，那样只装拖进来的这些。
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

set "INS=%~dp0install_models.py"
if not exist "%INS%" (
  echo   找不到 install_models.py。
  echo   它应该和本文件在同一个目录：%~dp0
  echo.
  pause
  exit /b 1
)

rem  %* = 拖进来的文件/文件夹路径。没有拖东西时是空的，脚本会去扫默认文件夹。
%PY% -X utf8 "%INS%" %*

echo.
pause
endlocal
