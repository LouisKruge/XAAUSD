' Stops the bridge, the engine and the dashboard.
'
' Only processes started from THIS installation's virtual environment are
' stopped: the WMI filter matches the .venv path, so another Python program
' on the machine is left alone.

Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")
Set wmi   = GetObject("winmgmts:\\.\root\cimv2")

root   = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
needle = LCase(root & "\.venv\scripts\python.exe")
stopped = 0

For Each p In wmi.ExecQuery("SELECT ProcessId, ExecutablePath FROM Win32_Process WHERE Name = 'python.exe'")
  If Not IsNull(p.ExecutablePath) Then
    If LCase(p.ExecutablePath) = needle Then
      On Error Resume Next
      p.Terminate()
      If Err.Number = 0 Then stopped = stopped + 1
      On Error Goto 0
    End If
  End If
Next

If stopped = 0 Then
  MsgBox "Nothing was running.", 64, "XAUUSD"
Else
  MsgBox "Stopped " & stopped & " process(es)." & vbCrLf & vbCrLf & _
         "Any open positions are UNCHANGED and still have their stops at the " & _
         "broker. Use Flatten in the dashboard first if you meant to close them.", _
         64, "XAUUSD"
End If
