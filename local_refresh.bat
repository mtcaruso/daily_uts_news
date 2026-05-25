@echo off
REM ============================================================
REM Self-host news refresh
REM
REM Roda no PC do user pra processar items que GHA bloqueia
REM (Canal Energia tem blacklist de IPs de datacenter).
REM
REM Setup necessario:
REM   1. Python 3.10+ com `py -m pip install -r requirements.txt`
REM   2. Variavel de ambiente GEMINI_API_KEY setada (System Properties
REM      -> Environment Variables -> User variables -> New)
REM   3. Git configurado com auth pro push (token ou ssh)
REM
REM Pra rodar manualmente: clique duas vezes nesse arquivo
REM Pra automatizar: agenda no Task Scheduler (ver README do repo)
REM ============================================================

setlocal
cd /d "%~dp0"

if "%GEMINI_API_KEY%"=="" (
    echo ERROR: variavel GEMINI_API_KEY nao esta setada
    echo Set via System Properties ^> Environment Variables
    pause
    exit /b 1
)

echo [1/4] git pull origin main...
git pull --ff-only origin main
if errorlevel 1 (
    echo ERROR: git pull falhou. Resolva conflito manualmente e tente de novo.
    pause
    exit /b 1
)

echo [2/4] python news_summarize.py...
py news_summarize.py
if errorlevel 1 (
    echo ERROR: news_summarize.py falhou
    pause
    exit /b 1
)

echo [3/4] git add + commit...
git add news_history.json news_diagnostic.json
git diff --staged --quiet
if errorlevel 1 (
    git commit -m "Local refresh [skip ci]"
    echo [4/4] git push...
    git push
    if errorlevel 1 (
        echo ERROR: git push falhou. Cheque auth.
        pause
        exit /b 1
    )
    echo SUCESSO: novos summaries pushed pro GitHub
) else (
    echo Nada novo pra commitar. Tudo em dia.
)

REM Se rodando interativo, pausa pra ver output
if "%CI%"=="" (
    if "%SCHEDULER_RUN%"=="" pause
)
