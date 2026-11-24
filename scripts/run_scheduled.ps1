param(
    [ValidateSet('school', 'all')][string]$Scope = 'school',
    [ValidateRange(0, 3650)][int]$SkipRecentDays = 7,
    [switch]$SchoolRotation,
    [ValidateRange(0, 10000)][int]$RotationTeachersPerUnit = 5,
    [ValidateSet('Default', 'On', 'Off')][string]$Attachments = 'Default'
)

$ErrorActionPreference = 'Stop'
$Scope = $Scope.ToLowerInvariant()
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$logDirectory = Join-Path $project 'data/logs'
[void][System.IO.Directory]::CreateDirectory($logDirectory)
$started = [DateTimeOffset]::Now
$log = Join-Path $logDirectory ('scheduled-{0:yyyyMM}.log' -f $started)
$argsForPython = @('cli.py', 'run-all', '--scope', $Scope, '--skip-recent-days', "$SkipRecentDays")
if ($SchoolRotation) {
    $argsForPython += @('--max-units', '0', '--max-teachers-per-unit', "$RotationTeachersPerUnit")
}
if ($Attachments -eq 'On') { $argsForPython += '--download-attachments' }
if ($Attachments -eq 'Off') { $argsForPython += '--no-download-attachments' }

$summary = $null
$diagnostics = ''
$code = 1
$process = [System.Diagnostics.Process]::new()
try {
    $startInfo = $process.StartInfo
    $startInfo.FileName = Join-Path $project '.venv/Scripts/python.exe'
    $startInfo.Arguments = $argsForPython -join ' '
    $startInfo.WorkingDirectory = $project
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $startInfo.StandardOutputEncoding = [Text.Encoding]::UTF8
    $startInfo.StandardErrorEncoding = [Text.Encoding]::UTF8
    $startInfo.EnvironmentVariables['PYTHONIOENCODING'] = 'utf-8'
    [void]$process.Start()
    $output = $process.StandardOutput.ReadToEndAsync()
    $errors = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $code = $process.ExitCode
    $diagnostics = $errors.GetAwaiter().GetResult().Trim()
    $body = $output.GetAwaiter().GetResult().Trim()
    if ($body) {
        try { $summary = ConvertFrom-Json -InputObject $body }
        catch {
            $diagnostics = "$diagnostics $body".Trim()
            if (!$code) { $code = 1 }
        }
    } elseif (!$code) {
        $diagnostics = 'The collector returned no summary.'
        $code = 1
    }
} catch {
    $code = 1
    $diagnostics = $_.Exception.Message
} finally {
    $process.Dispose()
}

$entry = [ordered]@{
    started_at = $started.ToString('o')
    finished_at = [DateTimeOffset]::Now.ToString('o')
    scope = $Scope
    school_rotation = [bool]$SchoolRotation
    exit_code = $code
    summary = $summary
    diagnostics = $diagnostics
}
$line = ConvertTo-Json -InputObject $entry -Depth 12 -Compress
[IO.File]::AppendAllText($log, $line + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output $line
exit $code
