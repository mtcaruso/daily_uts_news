' ============================================================
' Launcher silencioso do local_refresh.bat
'
' Roda o .bat 100% OCULTO (sem janela de cmd piscando), pro Windows
' Task Scheduler executar de forma invisível mesmo com a tela bloqueada
' (Win+L). Acha o .bat na mesma pasta deste script (portável).
'
' Uso no Task Scheduler:
'   Programa:   wscript.exe
'   Argumentos: "C:\...\utilities-news\local_refresh_silent.vbs"
' ============================================================
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\local_refresh.bat"

Set sh = CreateObject("WScript.Shell")
' Sinaliza pro .bat que está rodando via scheduler (não dá 'pause' no fim)
sh.Environment("PROCESS")("SCHEDULER_RUN") = "1"
' 0 = janela oculta, False = não espera terminar
sh.Run "cmd /c """ & batPath & """", 0, False
