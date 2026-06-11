' ============================================================
' Launcher silencioso do local_refresh.bat
'
' Roda o .bat 100% OCULTO (sem janela de cmd piscando), pro Windows
' Task Scheduler executar de forma invisivel mesmo com a tela bloqueada
' (Win+L).
'
' Executa uma COPIA do .bat no %TEMP%, nao o original. Motivo: o step 1 do
' bat faz `git pull`, que pode ATUALIZAR o proprio .bat — e o cmd le batch
' por offset de byte, entao modificar um .bat em execucao corrompe a run.
' Rodando a copia, o original pode ser atualizado livremente.
'
' >>> O NOME DA COPIA E UNICO POR RUN (timestamp). Isto e CRITICO: <<<
' uma copia em uso por uma run nao pode ser sobrescrita. Com nome fixo, o
' CopyFile falhava silenciosamente e a copia ficava CONGELADA numa versao
' antiga — todo run executava codigo velho mesmo apos commits novos do bat
' (foi exatamente o bug que mascarou as correcoes do lock/pause). Nome unico
' garante que cada run roda a versao ATUAL do bat. Copias antigas sao
' faxinadas no inicio (as em uso resistem ao delete, sem problema).
'
' O diretorio do repo vai como 1o argumento (a copia no TEMP nao pode usar
' %~dp0 pra achar o repo).
'
' Uso no Task Scheduler:
'   Programa:   wscript.exe
'   Argumentos: "C:\...\utilities-news\local_refresh_silent.vbs"
' ============================================================
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh = CreateObject("WScript.Shell")

scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
batPath = scriptDir & "\local_refresh.bat"
tempDir = sh.ExpandEnvironmentStrings("%TEMP%")

' --- Faxina: remove copias antigas utl_refresh_*.bat (as em uso resistem) ---
On Error Resume Next
Set folder = fso.GetFolder(tempDir)
For Each f In folder.Files
    If LCase(Left(f.Name, 12)) = "utl_refresh_" And LCase(fso.GetExtensionName(f.Name)) = "bat" Then
        fso.DeleteFile f.Path, True
    End If
Next
Err.Clear

' --- Nome unico por run: utl_refresh_AAAAMMDD_HHMMSS_mmm.bat ---
d = Now
ts = Year(d) & Right("0" & Month(d), 2) & Right("0" & Day(d), 2) & "_" & _
     Right("0" & Hour(d), 2) & Right("0" & Minute(d), 2) & Right("0" & Second(d), 2) & "_" & _
     Right("00" & (Int(Timer * 1000) Mod 1000), 3)
tmpBat = tempDir & "\utl_refresh_" & ts & ".bat"

fso.CopyFile batPath, tmpBat, True
If Err.Number <> 0 Then
    ' Copia falhou (raro com nome unico) — usa o original direto
    tmpBat = batPath
    Err.Clear
End If
On Error Goto 0

' Sinaliza pro .bat que esta rodando via scheduler (nao da 'pause' no fim)
sh.Environment("PROCESS")("SCHEDULER_RUN") = "1"
' 0 = janela oculta, False = nao espera terminar. Passa o dir do repo como arg.
sh.Run "cmd /c """"" & tmpBat & """ """ & scriptDir & """""", 0, False
