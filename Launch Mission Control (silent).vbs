' Starts Mission Control with no console window. Errors are written to .logs\launcher.log.
Set shell = CreateObject("WScript.Shell")
root = CreateObject("Scripting.FileSystemObject").GetParentFolderName(WScript.ScriptFullName)
shell.CurrentDirectory = root
shell.Run "cmd /c """ & root & "\Launch Mission Control.cmd""", 0, False
