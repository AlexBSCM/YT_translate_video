' YouTube Downloader - запуск без окна консоли/PowerShell
Set fso = CreateObject("Scripting.FileSystemObject")
Set ws = CreateObject("WScript.Shell")
dir = fso.GetParentFolderName(WScript.ScriptFullName)
ws.CurrentDirectory = dir
ws.Run "pythonw.exe """ & dir & "\yt_downloader.py""", 0, False
