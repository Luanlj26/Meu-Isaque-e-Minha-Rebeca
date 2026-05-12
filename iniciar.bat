@echo off
title Festival Meu Isaque e Minha Rebeca - Sistema
cd /d "%~dp0"
echo ========================================
echo   Festival Meu Isaque e Minha Rebeca
echo ========================================
echo   Iniciando servidor (producao)...
echo ========================================
start "" http://localhost:5000
python app.py --prod
pause
