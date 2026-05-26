@echo off
REM ============================================================
REM Self-host news + ANEEL aux + DOU refresh
REM
REM Roda no PC do user pra:
REM   1. Processar items que GHA bloqueia (CE Cloudflare, Liferay ANEEL)
REM   2. Backup pro DOU caso scheduled do GH falhe em disparar
REM
REM Inclui:
REM   - news_summarize.py (Canal Energia)
REM   - aneel_aux.py (Pautas RD via Liferay)
REM   - dou_mme.py --no-email (DOU MME/ANEEL atos oficiais; email vai via GHA)
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

echo [3/6] python aneel_aux.py (Pautas RD + Sala de Imprensa)...
py aneel_aux.py
if errorlevel 1 (
    echo WARN: aneel_aux.py falhou
)

echo [4/7] python dou_mme.py --no-email (DOU MME/ANEEL)...
REM --no-email pois RESEND_API_KEY só está nos GitHub Secrets. Email vai via GHA.
REM Backup pra quando scheduled do GH falhar em disparar.
py dou_mme.py --no-email
if errorlevel 1 (
    echo WARN: dou_mme.py falhou
)

echo [5/7] python sei_monitor.py (processos SEI)...
REM Visita URLs SEI com hash (em sei_processes.json), detecta novos andamentos
REM e notifica via ntfy. SEI nao requer login pra essas URLs publicas.
py sei_monitor.py
if errorlevel 1 (
    echo WARN: sei_monitor.py falhou
)

echo [6/7] git add + commit...
git add news_history.json news_diagnostic.json aneel_aux_history.json aneel_aux_diagnostic.json dou_history.json sei_processes.json
git diff --staged --quiet
if errorlevel 1 (
    git commit -m "Local refresh [skip ci]"
    echo [7/7] git push...
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
