' run_hidden.vbs -- launch powershell.exe invisibly (window style 0)
' Wedding-side copy (2026-09-25) of the Vault's launcher, so wedding crons don't depend on the property folder.
' Usage: wscript.exe //B //Nologo run_hidden.vbs <powershell_flags_and_script>
' All arguments after the VBS path are forwarded verbatim to powershell.exe.
' Example: wscript.exe //B //Nologo run_hidden.vbs -NoProfile -ExecutionPolicy Bypass -File "C:\...\run.ps1"

Dim args, cmd, i
Set args = WScript.Arguments

If args.Count = 0 Then
    WScript.Quit 1
End If

' Reconstruct the full argument string, quoting each arg that contains a space
cmd = ""
For i = 0 To args.Count - 1
    Dim a
    a = args(i)
    If InStr(a, " ") > 0 Then
        a = """" & a & """"
    End If
    If cmd = "" Then
        cmd = a
    Else
        cmd = cmd & " " & a
    End If
Next

Dim shell
Set shell = CreateObject("WScript.Shell")
' Window style 0 = hidden; bWaitOnReturn = False (fire and forget)
shell.Run "powershell.exe " & cmd, 0, False

Set shell = Nothing
