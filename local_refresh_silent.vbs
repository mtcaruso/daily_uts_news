' ============================================================
' Launcher silencioso do local_refresh.bat
'
' Roda o .bat 100% OCULTO (sem janela de cmd piscando), pro Windows
' Task Scheduler executar de forma invisível mesmo com a tela bloqueada
' (Win+L).
'
' IMPORTANTE: executa uma CÓPIA do .bat no %TEMP%, não o original. Motivo:
' o step 1 do bat faz `git pull`, que pode ATUALIZAR o próprio .bat — e o
' cmd lê batch files por offset de byte, então modificar um .bat em
' execução corrompe a run. Rodando a cópia, o original pode ser atualizado
' livremente (a versão nova vale a partir da run seguinte). O diretório do
' repo é passado como 1º argumento (a cópia no TEMP não pode usar %~dp0).
'
' Uso no Task Scheduler:
'   Programa:   wscript.exe
'   Argumentos: "C:\...\utilities-news\local_refresh_silent.vbs"
' ============================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\local_refresh.bat"
tmpBat = sh.ExpandEnvironmentStrings("%TEMP%") & "\local_refresh_run.bat"

' Copia o bat pro TEMP (sobrescreve se existir)
On Error Resume Next
fso.CopyFile batPath, tmpBat, True
If Err.Number <> 0 Then
    ' Cópia falhou (ex: run anterior ainda executando a cópia) — usa o original
    tmpBat = batPath
    Err.Clear
End If
On Error Goto 0

' Sinaliza pro .bat que está rodando via scheduler (não dá 'pause' no fim)
sh.Environment("PROCESS")("SCHEDULER_RUN") = "1"
' 0 = janela oculta, False = não espera terminar. Passa o dir do repo como arg.
sh.Run "cmd /c """"" & tmpBat & """ """ & scriptDir & """""", 0, False
