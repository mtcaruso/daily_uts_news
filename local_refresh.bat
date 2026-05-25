@echo off
REM ============================================================
REM Self-host news + ANEEL aux refresh
REM
REM Roda no PC do user pra processar items que GHA bloqueia
REM (Canal Energia e Liferay ANEEL tem blacklist de IPs de datacenter).
REM
REM Inclui:
REM   - news_summarize.py (Canal Energia)
REM   - aneel_aux.py (Pautas RD via Liferay)
REM
REM Setup necessario:
REM   1. Python 3.10+ com `py -m pip install -r requirements.txt`
REM   2. Variavel de ambiente GEMINI_API_KEY setada
REM   3. Git configurado com auth pro push
REM ============================================================

setlocal
cd /d "%~dp0"

if "%GEMINI_API_KEY%"=="" (
    echo ERROR: variavel GEMINI_API_KEY nao esta setada
    pause
    exit /b 1
)

echo [1/5] git pull origin main...
git pull --ff-only origin main
if errorlevel 1 (
    echo ERROR: git pull falhou
    pause
    exit /b 1
)

echo [2/5] python news_summarize.py (Canal Energia + outras)...
py news_summarize.py
if errorlevel 1 (
    echo WARN: news_summarize.py falhou — continuando pra aneel_aux
)

echo [3/5] python aneel_aux.py (Pautas RD + Sala de Imprensa)...
py aneel_aux.py
if errorlevel 1 (
    echo WARN: aneel_aux.py falhou
)

echo [4/5] git add + commit...
git add news_history.json news_diagnostic.json aneel_aux_history.json aneel_aux_diagnostic.json
git diff --staged --quiet
if errorlevel 1 (
    git commit -m "Local refresh [skip ci]"
    echo [5/5] git push...
    git push
    if errorlevel 1 (
        echo ERROR: git push falhou. Cheque auth.
        pause
        exit /b 1
    )
    echo SUCESSO: pushed pro GitHub
) else (
    echo Nada novo pra commitar. Tudo em dia.
)

REM Se rodando interativo, pausa pra ver output
if "%CI%"=="" (
    if "%SCHEDULER_RUN%"=="" pause
)
