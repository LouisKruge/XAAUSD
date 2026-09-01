' Starts the bridge, the engine and the dashboard without leaving console windows
' on screen, then opens the dashboard in the default browser.
'
' Launched by the "Start XAUUSD Bot" Desktop shortcut.

Set shell = CreateObject("WScript.Shell")
Set fso   = CreateObject("Scripting.FileSystemObject")

root = fso.GetParentFolderName(fso.GetParentFolderName(WScript.ScriptFullName))
shell.CurrentDirectory = root

python = root & "\.venv\Scripts\python.exe"
If Not fso.FileExists(python) Then
  MsgBox "The Python environment is missing." & vbCrLf & vbCrLf & _
         "Run Setup.bat in the windows folder first.", 16, "XAUUSD"
  WScript.Quit 1
End If

' 0 = hidden window, False = do not wait. The bridge must be up before the
' engine tries to reach it, so it goes first with a short pause after.
shell.Run """" & python & """ -m xauusd.cli bridge", 0, False
WScript.Sleep 4000
shell.Run """" & python & """ -m xauusd.cli run", 0, False
shell.Run """" & python & """ -m xauusd.cli dashboard", 0, False

' Give uvicorn a moment to bind before the browser asks for the page.
WScript.Sleep 4000
shell.Run "http://127.0.0.1:8000", 1, False
