@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion
title 动态云防火墙 · IP 放行管理平台

cd /d "%~dp0"

echo ============================================================
echo   动态云防火墙 · IP 放行管理平台   Windows 一键启动
echo ============================================================
echo.

rem ---------- 1. 定位 Python ----------
set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
    where py >nul 2>nul && set "PY=py -3"
)
if not defined PY (
    echo [错误] 未检测到 Python，请先安装 Python 3.9+ 并勾选 "Add to PATH"
    echo        下载地址：https://www.python.org/downloads/windows/
    pause
    exit /b 1
)

for /f "tokens=2" %%v in ('%PY% -V 2^>^&1') do set "PYVER=%%v"
echo [1/4] Python 版本：!PYVER!

rem ---------- 2. 创建虚拟环境 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [2/4] 首次运行，正在创建虚拟环境 .venv ...
    %PY% -m venv .venv
    if errorlevel 1 (
        echo [错误] 虚拟环境创建失败
        pause
        exit /b 1
    )
) else (
    echo [2/4] 虚拟环境已存在
)

set "VPY=.venv\Scripts\python.exe"

rem ---------- 3. 安装依赖 ----------
"%VPY%" -c "import flask, apscheduler, alibabacloud_swas_open20200601" >nul 2>nul
if errorlevel 1 (
    echo [3/4] 正在安装依赖（使用阿里云镜像加速）...
    "%VPY%" -m pip install --upgrade pip -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
    "%VPY%" -m pip install -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/ --trusted-host mirrors.aliyun.com
    if errorlevel 1 (
        echo [错误] 依赖安装失败，请检查网络后重试
        pause
        exit /b 1
    )
) else (
    echo [3/4] 依赖已就绪
)

rem ---------- 4. 启动服务 ----------
if not exist "logs" mkdir logs
if not exist "data" mkdir data

echo [4/4] 启动服务...
echo.
echo   管理后台：http://127.0.0.1:5050
echo   默认账号：admin / admin123（登录后请及时修改）
echo   按 Ctrl+C 停止服务
echo.

"%VPY%" run.py %*

echo.
echo 服务已停止。
pause
