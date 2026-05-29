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
REM   - sei_monitor.py (processos SEI)
REM   - mme_legislacao.py (portarias/decretos/leis MME)
REM   - cvm_realtime.py (IPE — FRs, Comunicados, VLMO insiders)
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

echo [1/9] git pull --rebase origin main...
REM --rebase -X theirs absorve commits do GHA mesmo quando branches divergiram
REM (ex: commit local de run anterior que nao pushou). --ff-only travava nesse caso.
git pull --rebase -X theirs origin main
if errorlevel 1 (
    echo WARN: rebase falhou — tentando abortar + merge -X ours
    git rebase --abort
    git pull --no-rebase -X ours origin main
    if errorlevel 1 (
        echo ERROR: git pull falhou de vez. Resolva conflito manual.
        pause
        exit /b 1
    )
)

echo [2/9] python news_summarize.py (Canal Energia + outras)...
py news_summarize.py
if errorlevel 1 (
    echo WARN: news_summarize.py falhou — continuando pra aneel_aux
)

echo [3/9] python aneel_aux.py (Pautas RD + Sala de Imprensa)...
py aneel_aux.py
if errorlevel 1 (
    echo WARN: aneel_aux.py falhou
)

echo [4/9] python dou_mme.py --no-email (DOU MME/ANEEL)...
REM --no-email pois RESEND_API_KEY só está nos GitHub Secrets. Email vai via GHA.
REM Backup pra quando scheduled do GH falhar em disparar.
py dou_mme.py --no-email
if errorlevel 1 (
    echo WARN: dou_mme.py falhou
)

echo [5/9] python sei_monitor.py (processos SEI)...
REM Visita URLs SEI com hash (em sei_processes.json), detecta novos andamentos
REM e notifica via ntfy. SEI nao requer login pra essas URLs publicas.
py sei_monitor.py
if errorlevel 1 (
    echo WARN: sei_monitor.py falhou
)

echo [6/9] python mme_legislacao.py (portarias/decretos/leis MME)...
REM Coleta legislacao MME via Plone publico. Resume ementas via Gemini.
REM Notifica via ntfy itens novos (apos primeira run).
py mme_legislacao.py
if errorlevel 1 (
    echo WARN: mme_legislacao.py falhou
)

echo [7/9] python cvm_realtime.py (CVM IPE — FRs, Comunicados, VLMO)...
REM Scraper CVM RAD em tempo real. Backup pro cvm-realtime.yml workflow
REM (que tenta a cada 5min mas pode ser dropado pelo GHA).
py cvm_realtime.py
if errorlevel 1 (
    echo WARN: cvm_realtime.py falhou
)

echo [8/9] git add + commit...
git add news_history.json news_diagnostic.json aneel_aux_history.json aneel_aux_diagnostic.json dou_history.json sei_processes.json mme_legislacao_history.json mme_legislacao_diagnostic.json cvm_recent.json alerts_state.json
git diff --staged --quiet
if errorlevel 1 (
    git commit -m "Local refresh [skip ci]"
    echo [9/9] git pull --rebase + push...
    REM Rebase antes do push pra absorver commits que o GHA pushou DURANTE a run
    REM (race condition). Pra JSONs de estado, conflito resolve sempre pela versao
    REM local (-X theirs) — sao regenerados a cada run, entao nao perde nada real.
    git pull --rebase -X theirs origin main
    if errorlevel 1 (
        echo WARN: rebase com conflito inesperado — tentando abortar e merge simples
        git rebase --abort
        git pull --no-rebase -X ours origin main
    )
    git push
    if errorlevel 1 (
        echo WARN: 1o push falhou, tentando pull --rebase + push de novo...
        git pull --rebase -X theirs origin main
        git push
        if errorlevel 1 (
            echo ERROR: git push falhou apos retry. Cheque auth/conflito manual.
            pause
            exit /b 1
        )
    )
    echo SUCESSO: pushed pro GitHub
) else (
    echo Nada novo pra commitar. Tudo em dia.
)

REM Se rodando interativo, pausa pra ver output
if "%CI%"=="" (
    if "%SCHEDULER_RUN%"=="" pause
)
