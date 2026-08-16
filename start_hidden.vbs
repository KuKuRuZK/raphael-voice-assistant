' Runs start.bat fully in background (no watchdog window).
' Double-click this file = silent launch of Lin with auto-restart.
Set ws = CreateObject("WScript.Shell")
ws.Run """" & CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName) & "\start.bat""", 0, False
