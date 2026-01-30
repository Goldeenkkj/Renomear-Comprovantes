@echo off
chcp 65001 >nul
title Renomear Comprovantes v3 - Farmausa
echo ===============================================
echo      RENOMEAR COMPROVANTES - VERSAO 3
echo ===============================================
echo.

REM --- Caminho da pasta onde o script está ---
cd /d "%~dp0"

REM --- Verifica se o Python está instalado ---
python --version >nul 2>&1
if errorlevel 1 (
    echo [ERRO] Python não encontrado no sistema.
    echo Por favor, instale o Python em https://www.python.org/downloads/
    echo e marque a opção "Add Python to PATH" durante a instalação.
    pause
    exit /b
)

echo Verificando dependências...

echo.
echo Executando o script de renomeação...
echo ===============================================
echo.

python renomear_comprovantes_v3.py

echo.
echo ===============================================
echo Processo concluído!
echo Os comprovantes renomeados estão na pasta 'saida'
echo e o arquivo ZIP foi gerado como 'comprovantes_renomeados_v3.zip'.
echo.
pause
