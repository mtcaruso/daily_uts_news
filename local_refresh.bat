@echo off
REM ============================================================
REM Self-host: DOU + ANEEL + SEI + MME + CVM + news refresh
REM
REM Roda no PC do user (Task Scheduler "UtilitiesBR-Refresh", via
REM local_refresh_silent.vbs — que executa uma COPIA deste bat no TEMP, pra
REM o git pull do step 1 poder atualizar este arquivo sem corromper a
REM execucao em andamento; cmd le batch por offset de byte).
REM
REM ===== DUAS BLINDAGENS CRITICAS (aprendidas na marra) =====
REM 1. LOCK DE INSTANCIA UNICA: news_summarize pode passar do intervalo do
REM    scheduler; sem lock, 2 runs rodavam concorrentes e BRIGAVAM pelo git
REM    (push falhava, runs travavam, zumbis se acumulavam). Agora a 2a
REM    instancia falha ao abrir o lockfile com handle exclusivo e sai limpo.
REM 2. NUNCA DAR `pause` SOB SCHEDULER: rodando oculto (sem console), um
REM    `pause` numa trilha de erro travava o cmd PARA SEMPRE esperando tecla
REM    -> dezenas de processos cmd zumbis segurando o git. Agora so ha UM
REM    pause, no fim, guardado por SCHEDULER_RUN (so em run manual).
REM
REM ORDEM: dados regulatorios (DOU/ANEEL/SEI/MME/CVM) ANTES de news_summarize
REM — news e o step mais demorado (backlog + throttle Gemini ~4.5s/call) e o
REM menos critico de manha; o DOU do dia fica pronto nos primeiros minutos.
REM
REM Setup necessario:
REM   1. Python 3.10+ com `py -m pip install -r requirements.txt`
REM   2. Variavel de ambiente GEMINI_API_KEY setada
REM   3. Git configurado com auth pro push
REM ============================================================

setlocal

REM Dir do repo: 1o argumento (passado pelo VBS, pois a copia roda no TEMP);
REM fallback pro dir deste arquivo (execucao manual direto do repo).
if not "%~1"=="" (cd /d "%~1") else (cd /d "%~dp0")

REM ---- LOCK DE INSTANCIA UNICA --------------------------------------------
REM Abre o lockfile com handle exclusivo (9>). Se outra instancia ja o segura,
REM o redirect falha, o `||` dispara e saimos sem tocar no git. O handle e
REM liberado automaticamente pelo SO quando esta instancia termina OU e morta
REM (entao um zumbi nunca mais bloqueia eternamente — o lock se cura sozinho).
set "LOCKFILE=%TEMP%\utilities_refresh.lock"
2>nul ( 9>"%LOCKFILE%" call :run ) || (
    echo [lock] Outra instancia ja esta rodando. Saindo sem fazer nada.
)
goto :finish

REM =========================== CORPO ===================================
:run
if "%GEMINI_API_KEY%"=="" (
    echo ERROR: variavel GEMINI_API_KEY nao esta setada
    REM exit /b 0 de proposito: nao e "lock ocupado", so nada a fazer.
    exit /b 0
)

echo [1/8] salvage de run interrompida + git pull --rebase...
REM salvage_state.py: preserva (commita) state JSONs validos de uma run
REM anterior interrompida e descarta apenas os corrompidos. Antes, TUDO era
REM descartado — jogando fora resumos Gemini que ja gastaram cota.
py salvage_state.py

REM --rebase -X theirs absorve commits do GHA mesmo quando branches divergiram.
REM --autostash protege contra working tree sujo (ex: arquivo nao-state mexido).
git pull --rebase -X theirs --autostash origin main
if errorlevel 1 (
    echo WARN: rebase falhou — abortando + merge -X ours
    git rebase --abort 2>nul
    git pull --no-rebase -X ours origin main
    if errorlevel 1 (
        echo ERROR: git pull falhou de vez. Resolva conflito manual no proximo run.
        exit /b 0
    )
)

echo [2/8] python dou_mme.py --no-email (DOU MME/ANEEL)...
REM PRIMEIRO da fila: dado mais valioso de manha. --no-email pois
REM RESEND_API_KEY so esta nos GitHub Secrets (email vai via GHA).
py dou_mme.py --no-email
if errorlevel 1 echo WARN: dou_mme.py falhou

echo [3/8] python aneel_aux.py (Pautas RD + Sala de Imprensa + CP/AP/TS)...
py aneel_aux.py
if errorlevel 1 echo WARN: aneel_aux.py falhou

echo [4/8] python sei_monitor.py (processos SEI)...
REM Detecta novos andamentos e notifica via ntfy (independente do commit).
py sei_monitor.py
if errorlevel 1 echo WARN: sei_monitor.py falhou

echo [5/8] python mme_legislacao.py (portarias/decretos/leis MME)...
py mme_legislacao.py
if errorlevel 1 echo WARN: mme_legislacao.py falhou

echo [6/8] python cvm_realtime.py (CVM IPE — FRs, Comunicados, VLMO)...
py cvm_realtime.py
if errorlevel 1 echo WARN: cvm_realtime.py falhou

echo [7/8] python news_summarize.py (Canal Energia + outras)...
REM ULTIMO scraper: e o mais demorado (pode levar 10-20min com backlog).
REM Se a run for interrompida aqui, todo o regulatorio acima ja foi salvo
REM localmente e o salvage da proxima run preserva.
py news_summarize.py
if errorlevel 1 echo WARN: news_summarize.py falhou

echo [8/8] git add + commit + push...
git add news_history.json news_diagnostic.json aneel_aux_history.json aneel_aux_diagnostic.json dou_history.json sei_processes.json mme_legislacao_history.json mme_legislacao_diagnostic.json cvm_recent.json alerts_state.json
git diff --staged --quiet
if errorlevel 1 (
    git commit -m "Local refresh [skip ci]"
    REM Rebase antes do push pra absorver commits que o GHA pushou DURANTE a
    REM run. Conflitos em state JSONs resolvem pela versao local (-X theirs).
    git pull --rebase -X theirs --autostash origin main
    if errorlevel 1 (
        echo WARN: rebase com conflito inesperado — abortando e merge simples
        git rebase --abort 2>nul
        git pull --no-rebase -X ours origin main
    )
    git push
    if errorlevel 1 (
        echo WARN: 1o push falhou, tentando pull --rebase + push de novo...
        git pull --rebase -X theirs --autostash origin main
        git push
        if errorlevel 1 echo ERROR: git push falhou apos retry. Cheque auth/conflito.
    )
    echo SUCESSO: pushed pro GitHub
) else (
    echo Nada novo pra commitar. Tudo em dia.
)
exit /b 0

REM =========================== FIM ===================================
:finish
REM Pausa SO em execucao manual interativa (NUNCA sob scheduler/CI — la,
REM um pause travaria o processo oculto pra sempre, que era o bug original).
if "%CI%"=="" if "%SCHEDULER_RUN%"=="" pause
endlocal
