param(
    [ValidateSet('Install', 'Status', 'Uninstall')][string]$Action = 'Status',
    [ValidateSet('school', 'all')][string]$Scope = 'school',
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')][string]$DailyAt = '03:10',
    [ValidateSet('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday')]
    [string]$WeeklyDay = 'Sunday',
    [ValidatePattern('^(?:[01]\d|2[0-3]):[0-5]\d$')][string]$WeeklyAt = '04:30',
    [ValidateRange(0, 3650)][int]$SkipRecentDays = 7,
    [ValidateRange(0, 10000)][int]$WeeklyTeachersPerUnit = 5,
    [ValidateSet('Default', 'On', 'Off')][string]$Attachments = 'Default',
    [switch]$DailyOnly,
    [PSCredential]$Credential,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$project = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$runner = Join-Path $PSScriptRoot 'run_scheduled.ps1'
$exe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
$common = '-NoLogo -NoProfile -NonInteractive -WindowStyle Hidden -File "{0}" -SkipRecentDays {1} -Attachments {2}' -f (
    $runner, $SkipRecentDays, $Attachments
)
$plans = @(
    [pscustomobject]@{
        name = 'ECNUWorkbench-Daily'
        cadence = 'daily'
        at = $DailyAt
        day = $null
        arguments = "$common -Scope $Scope"
        install = $true
    }
    [pscustomobject]@{
        name = 'ECNUWorkbench-WeeklySchool'
        cadence = 'weekly'
        at = $WeeklyAt
        day = $WeeklyDay
        arguments = "$common -Scope school -SchoolRotation -RotationTeachersPerUnit $WeeklyTeachersPerUnit"
        install = !$DailyOnly
    }
)
$previewAction = $Action
if ($DryRun -and !$PSBoundParameters.ContainsKey('Action')) { $previewAction = 'Install' }
if ($DryRun) {
    foreach ($plan in $plans) {
        $operation = if ($previewAction -eq 'Install' -and !$plan.install) { 'Uninstall' } else { $previewAction }
        [pscustomobject]@{
            action = $operation
            task = $plan.name
            cadence = $plan.cadence
            day = $plan.day
            local_time = $plan.at
            working_directory = $project
            command = '"{0}" {1}' -f $exe, $plan.arguments
            run_when_logged_off = [bool]$Credential
        }
    }
    return
}

foreach ($plan in $plans) {
    if ($Action -eq 'Status') {
        $task = Get-ScheduledTask -TaskName $plan.name -TaskPath '\' -ErrorAction SilentlyContinue
        if ($task) {
            $info = Get-ScheduledTaskInfo -InputObject $task
            [pscustomobject]@{ TaskName = $plan.name; State = $task.State; NextRun = $info.NextRunTime; LastResult = $info.LastTaskResult }
        } else {
            [pscustomobject]@{ TaskName = $plan.name; State = 'NotInstalled'; NextRun = $null; LastResult = $null }
        }
        continue
    }
    if ($Action -eq 'Uninstall' -or !$plan.install) {
        if (Get-ScheduledTask -TaskName $plan.name -TaskPath '\' -ErrorAction SilentlyContinue) {
            Unregister-ScheduledTask -TaskName $plan.name -TaskPath '\' -Confirm:$false
        }
        continue
    }
    $start = [DateTime]::Today.Add([TimeSpan]::ParseExact($plan.at, 'hh\:mm', [Globalization.CultureInfo]::InvariantCulture))
    $trigger = if ($plan.cadence -eq 'daily') {
        New-ScheduledTaskTrigger -Daily -At $start
    } else {
        New-ScheduledTaskTrigger -Weekly -DaysOfWeek $plan.day -At $start
    }
    $settings = @{
        StartWhenAvailable = $true
        MultipleInstances = 'IgnoreNew'
        AllowStartIfOnBatteries = $true
        DontStopIfGoingOnBatteries = $true
        ExecutionTimeLimit = New-TimeSpan -Hours 6
    }
    $setup = @{
        TaskName = $plan.name
        TaskPath = '\'
        Action = New-ScheduledTaskAction -Execute $exe -Argument $plan.arguments -WorkingDirectory $project
        Trigger = $trigger
        Settings = New-ScheduledTaskSettingsSet @settings
        Force = $true
    }
    if ($Credential) {
        Register-ScheduledTask @setup -User $Credential.UserName -Password $Credential.GetNetworkCredential().Password -RunLevel Limited
    } else {
        $account = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
        $principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask @setup -Principal $principal
    }
}
