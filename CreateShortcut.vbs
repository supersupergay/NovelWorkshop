Option Explicit
Dim shell, fso, root, exe, shortcut
Set shell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
root = fso.GetParentFolderName(WScript.ScriptFullName)
exe = fso.BuildPath(root, "NovelWorkshop_2026.11.14.1.exe")
If Not fso.FileExists(exe) Then
    MsgBox "Please extract all files into the same folder.", 48, "Novel Workshop"
    WScript.Quit 1
End If
Set shortcut = shell.CreateShortcut(fso.BuildPath(root, "NovelWorkshop.lnk"))
shortcut.TargetPath = exe
shortcut.WorkingDirectory = root
shortcut.IconLocation = fso.BuildPath(root, "icon.ico") & ",0"
shortcut.Description = "Novel Workshop"
shortcut.Save
MsgBox "NovelWorkshop.lnk created in this folder.", 64, "Novel Workshop"
