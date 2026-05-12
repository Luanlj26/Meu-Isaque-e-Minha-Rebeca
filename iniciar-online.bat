@echo off
title Festival Meu Isaque e Minha Rebeca - ONLINE
cd /d "%~dp0"
echo ========================================
echo   FESTIVAL MEU ISAQUE E MINHA REBECA
echo ========================================
echo   Iniciando com acesso PUBLICO...
echo ========================================
echo.
echo   O ngrok criara um link publico para
echo   que qualquer pessoa acesse de qualquer lugar.
echo.
echo   Se tiver plano pago ngrok, o link sera:
echo   https://festival-isaeque-rebeca.ngrok-free.app
echo.
echo   Primeiro uso? Crie conta gratis em:
echo   https://dashboard.ngrok.com/signup
echo.
echo   Depois pegue seu token em:
echo   https://dashboard.ngrok.com/get-started/your-authtoken
echo.
python app.py --online --prod
pause
