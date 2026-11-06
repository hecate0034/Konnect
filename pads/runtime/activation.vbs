' PADS activation evidence only. No document or library calls.
Option Explicit
Dim progid, kind, app, code, detail, value, cleanupCode
progid = WScript.Arguments(0)
kind = WScript.Arguments(1)

Function Quote(s)
  s = Replace(CStr(s), "\", "\\")
  s = Replace(s, Chr(34), "\" & Chr(34))
  s = Replace(s, vbCr, "\r")
  s = Replace(s, vbLf, "\n")
  s = Replace(s, vbTab, "\t")
  Quote = Chr(34) & s & Chr(34)
End Function

Sub Emit(stage, status, number, message)
  WScript.Echo "{""stage"":" & Quote(stage) & ",""status"":" & Quote(status) & _
    ",""hresult"":" & Quote(Hex(number)) & ",""detail"":" & Quote(message) & "}"
End Sub

Emit "activate", "started", 0, progid
On Error Resume Next
Set app = CreateObject(progid)
code = Err.Number
detail = Err.Description
Err.Clear
If code <> 0 Then
  Emit "result", "activation_failed", code, detail
  WScript.Quit 0
End If
Emit "activate", "activated", 0, TypeName(app)
If kind = "application" Then
  app.Visible = False
  Emit "visibility", "observed", Err.Number, Err.Description
  Err.Clear
  value = app.Version
  code = Err.Number
  detail = Err.Description
  Err.Clear
  If code = 0 Then
    Emit "version", "read_success", 0, value
  Else
    Emit "version", "read_failed", code, detail
  End If
  app.Quit
  cleanupCode = Err.Number
  detail = Err.Description
  Err.Clear
  Emit "cleanup", "quit_returned", cleanupCode, detail
End If
Set app = Nothing
If cleanupCode <> 0 Then
  Emit "result", "activation_succeeded_cleanup_failed", cleanupCode, "Inspect the created application"
Else
  Emit "result", "activation_only_pass", 0, "No project or library was opened"
End If
