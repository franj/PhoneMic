$ProjectRoot = $PSScriptRoot

$Desktop = [Environment]::GetFolderPath("Desktop")
$ShortcutPath = Join-Path $Desktop "PhoneMic Dev.lnk"

$WshShell = New-Object -ComObject WScript.Shell
$Shortcut = $WshShell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath       = "$ProjectRoot\.venv\Scripts\pythonw.exe"
$Shortcut.Arguments        = "-m phonemic.PhoneMic"
$Shortcut.WorkingDirectory = $ProjectRoot
$Shortcut.Description      = "PhoneMic (from source)"
$Shortcut.IconLocation     = "$ProjectRoot\phonemic\resources\favicon.ico,0"
$Shortcut.Save()

Write-Host "Shortcut created: $ShortcutPath"