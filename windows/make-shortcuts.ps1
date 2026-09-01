# Creates the Desktop shortcuts. Called by Setup.bat; not run directly.
#
# The shortcuts point at the .vbs launchers rather than the .bat files so that
# starting the bot does not leave console windows on screen. The dashboard is
# the interface; the processes behind it should be invisible.

param([Parameter(Mandatory = $true)][string]$Root)

$desktop = [Environment]::GetFolderPath('Desktop')
$shell = New-Object -ComObject WScript.Shell

function New-Shortcut {
    param($Name, $Target, $Arguments, $Description, $IconIndex)
    $path = Join-Path $desktop "$Name.lnk"
    $sc = $shell.CreateShortcut($path)
    $sc.TargetPath = $Target
    $sc.Arguments = $Arguments
    $sc.WorkingDirectory = $Root
    $sc.Description = $Description
    $sc.IconLocation = "$env:SystemRoot\System32\shell32.dll,$IconIndex"
    $sc.Save()
    Write-Host "      $Name"
}

New-Shortcut -Name 'Start XAUUSD Bot' `
    -Target 'wscript.exe' `
    -Arguments "`"$Root\windows\start.vbs`"" `
    -Description 'Start the trading engine, the MT5 bridge and the dashboard' `
    -IconIndex 137

New-Shortcut -Name 'Stop XAUUSD Bot' `
    -Target 'wscript.exe' `
    -Arguments "`"$Root\windows\stop.vbs`"" `
    -Description 'Stop the trading engine, the MT5 bridge and the dashboard' `
    -IconIndex 27

# Live arming stays a deliberate act at the machine, with a visible console and
# typed confirmations. It is key 2 of the two-key design: routing it through the
# dashboard would collapse both keys into one channel.
New-Shortcut -Name 'Arm Live Trading' `
    -Target "$env:SystemRoot\System32\cmd.exe" `
    -Arguments "/c `"$Root\windows\Arm Live Trading.bat`"" `
    -Description 'Authorise real-money trading (asks for your account number twice)' `
    -IconIndex 77
