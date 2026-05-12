@echo off
title Festival Meu Isaque e Minha Rebeca
cd /d "%~dp0"
echo ========================================
echo   FESTIVAL MEU ISAQUE E MINHA REBECA
echo ========================================
echo   Matando servidor antigo (porta 5000)...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5000 "') do (
    if not "%%a"=="" taskkill /f /pid %%a >nul 2>&1
)
timeout /t 1 /nobreak >nul
echo   Iniciando servidor com AUTO-RECARREGAMENTO
echo   Qualquer alteracao nos arquivos atualiza sozinho!
echo.
echo   Local:  http://localhost:5000
echo.
python app.py --online
pause