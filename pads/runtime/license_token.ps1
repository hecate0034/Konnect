# Private licensing bridge. Request/response stay in pipes; never log tokens.
$ErrorActionPreference='Stop'
$ProgressPreference='SilentlyContinue'
$server=$null
try {
    $request=[Console]::In.ReadToEnd() | ConvertFrom-Json
    $server=New-Object -ComObject MGCPCBAutomationLicensing.Application
    @{token=$server.GetToken($request.key)} | ConvertTo-Json -Compress
} finally {
    if ($server) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($server) }
}
