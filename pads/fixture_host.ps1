# Acceptance harness only. Opens the copied official sample, outside MCP tools.
param([ValidateSet('designer','layout')][string]$Domain, [string]$Fixture, [string]$Control,
      [ValidateRange(1,120)][int]$LifetimeMinutes=20)
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$processName = if ($Domain -eq 'designer') {'viewdraw'} else {'ExpeditionPE'}
if (Get-Process -Name $processName -ErrorAction SilentlyContinue) { throw 'Existing editor; refusing fixture launch' }
if (-not (Test-Path -LiteralPath (Join-Path $Fixture 'source-manifest.json'))) { throw 'Prepared fixture required' }
$app=$null
try {
    $progid = if ($Domain -eq 'designer') {'ViewDraw.Application.66'} else {'MGCPCB.ExpeditionPEApplication.66'}
    @{stage='activation_started'; domain=$Domain} | ConvertTo-Json -Compress
    $app = New-Object -ComObject $progid
    @{stage='activated'; domain=$Domain} | ConvertTo-Json -Compress
    $app.Visible=$true
    @{stage='opening_fixture'; domain=$Domain} | ConvertTo-Json -Compress
    if ($Domain -eq 'designer') {
        [void]$app.OpenProject((Join-Path $Fixture 'LessonFinal/LessonFinal.prj'))
        $design=$app.GetProjectData().GetiCDBDesigns().GetItem(1)
        $documents=$app.SchematicSheetDocuments()
        $sheets=$documents.GetAvailableSheets($design)
        if ($sheets.GetCount() -lt 1) { throw 'Existing sample sheet required' }
        [void]$documents.Open($design,$sheets.GetItem(1))
    }
    else { [void]$app.OpenDocument((Join-Path $Fixture 'LessonFinal/PCB/CORPORATE.pcb')) }
    @{stage='opened'; domain=$Domain} | ConvertTo-Json -Compress
    $deadline=[DateTime]::UtcNow.AddMinutes($LifetimeMinutes)
    while ([DateTime]::UtcNow -lt $deadline) {
        if (Test-Path -LiteralPath $Control) {
            $command=Get-Content -LiteralPath $Control -Raw
            Remove-Item -LiteralPath $Control
            if ($command.Trim() -eq 'quit') { break }
            if ($command.Trim() -eq 'close' -and $Domain -eq 'designer') { $app.CloseProject() }
        }
        Start-Sleep -Milliseconds 250
    }
} finally {
    if ($null -ne $app) {
        # Never close an editor whose target was changed outside this host.
        $expected = if ($Domain -eq 'designer') {(Join-Path $Fixture 'LessonFinal/LessonFinal.prj')} else {(Join-Path $Fixture 'LessonFinal/PCB/CORPORATE.pcb')}
        try {
            $current = if ($Domain -eq 'designer') {$app.GetProjectData().GetProjectFilePath()} else {$app.ActiveDocument.FullName}
            if ($current -eq $expected) { $app.Quit() }
            else { Write-Output '{"stage":"cleanup_skipped","reason":"target_changed_or_unavailable"}' }
        } catch { Write-Output '{"stage":"cleanup_unverified"}' }
    }
}
