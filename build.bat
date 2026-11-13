@echo off
rem ============================================================================
rem  PortOS - rebuild images, clear old sessions, and start the gateway.
rem
rem  The session image carries the whole apps\ directory, so after a rebuild
rem  every new session container has the published works on disk.
rem
rem  NOTE: cmd.exe mis-seeks the file after a line that ends with a
rem  multi-byte character, so every line here ends with plain ASCII.
rem ============================================================================
chcp 65001 >nul
setlocal

cd /d "%~dp0"

set "SESSION_IMAGE=portos/session:latest"

set "PORTOS_HOST_TZ="
for /f "delims=" %%i in ('powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File scripts\host-timezone.ps1') do set "PORTOS_HOST_TZ=%%i"
if defined PORTOS_HOST_TZ echo 宿主机时区为 %PORTOS_HOST_TZ%.
if not defined PORTOS_HOST_TZ echo 无法读取宿主机时区，将使用 PORTOS_TZ 或 UTC.

where docker >nul 2>&1
if errorlevel 1 set "PATH=%LOCALAPPDATA%\Programs\DockerDesktop\resources\bin;%PATH%"

rem --------------------------------------------------------------- 0. the daemon
echo.
echo [0] 正在检查 Docker ...
call :wait_docker
if errorlevel 1 goto :failed

rem --------------------------------------------------------------- 1. the image
echo.
echo [1] 正在重建会话镜像 %SESSION_IMAGE% ...
docker build -f session\Dockerfile -t %SESSION_IMAGE% .
if errorlevel 1 goto :failed

echo.
echo [2] 正在重建服务端镜像 gateway ...
docker compose build gateway
if errorlevel 1 goto :failed

rem ---------------------------------------------------- 3. the old containers
echo.
echo [3] 正在清掉旧的会话容器（它们跑的还是旧镜像）...
for /f "tokens=*" %%i in ('docker ps -aq --filter "label=portos.session=1"') do docker rm -f %%i >nul 2>&1
echo       清完了，服务端几秒后会补上新的.

rem --------------------------------------------------------------- 4. the server
echo.
echo [4] 正在重启服务端 ...
docker compose up -d --remove-orphans gateway
if errorlevel 1 goto :failed
call :wait_healthy
if errorlevel 1 goto :failed

echo.
echo 重建完成，打开 http://localhost:8081 即可.
goto :done

rem ============================================================================
rem  subroutines
rem ============================================================================

rem Wait for the daemon; start Docker Desktop if it is not up yet.
:wait_docker
docker info >nul 2>&1
if not errorlevel 1 exit /b 0
echo       Docker 没在运行，正在尝试启动 Docker Desktop ...
if exist "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe" (
    start "" "%LOCALAPPDATA%\Programs\DockerDesktop\Docker Desktop.exe"
) else if exist "%ProgramFiles%\Docker\Docker\Docker Desktop.exe" (
    start "" "%ProgramFiles%\Docker\Docker\Docker Desktop.exe"
) else (
    echo [错误] 找不到 Docker Desktop，请先手动启动再重试.
    exit /b 1
)
for /l %%i in (1,1,60) do (
    ping -n 4 127.0.0.1 >nul
    docker info >nul 2>&1 && exit /b 0
)
echo [错误] Docker 启动超时.
exit /b 1

rem Wait until the server container reports healthy.
:wait_healthy
for /l %%i in (1,1,30) do (
    ping -n 3 127.0.0.1 >nul
    for /f "delims=" %%h in ('docker inspect --format "{{.State.Health.Status}}" gateway 2^>nul') do (
        if "%%h"=="healthy" exit /b 0
    )
)
echo [错误] 服务端没有变成 healthy.
exit /b 1

rem ============================================================================
:failed
echo.
echo 重建失败.
if not "%PORTOS_NO_PAUSE%"=="" exit /b 1
pause
exit /b 1

:done
if not "%PORTOS_NO_PAUSE%"=="" exit /b 0
pause
exit /b 0
