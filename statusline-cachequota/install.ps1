[CmdletBinding()]
param(
    [string]$ClaudeConfigDir,
    [string]$PythonExe
)

$ErrorActionPreference = 'Stop'

if (-not $ClaudeConfigDir) {
    if ($env:CLAUDE_CONFIG_DIR) {
        $ClaudeConfigDir = $env:CLAUDE_CONFIG_DIR
    } else {
        $ClaudeConfigDir = Join-Path $HOME '.claude'
    }
}

if (-not $PythonExe) {
    $python = Get-Command python -ErrorAction SilentlyContinue
    if ($python) {
        $PythonExe = $python.Source
    } else {
        throw 'Python 3 is required. Pass its path with -PythonExe.'
    }
}

if (-not (Test-Path -LiteralPath $PythonExe -PathType Leaf)) {
    throw "Python executable not found: $PythonExe"
}

$version = & $PythonExe -c 'import sys; print(sys.version_info.major)'
if ($LASTEXITCODE -ne 0 -or $version -ne '3') {
    throw "Python 3 is required: $PythonExe"
}

New-Item -ItemType Directory -Force -Path $ClaudeConfigDir | Out-Null
$renderer = Join-Path $ClaudeConfigDir 'statusline-render.py'
Copy-Item -LiteralPath (Join-Path $PSScriptRoot 'statusline-render.py') -Destination $renderer -Force

$payload = '{"model":{"display_name":"Claude"},"context_window":{"used_percentage":42}}'
$startInfo = New-Object System.Diagnostics.ProcessStartInfo
$startInfo.FileName = $PythonExe
$startInfo.Arguments = "-X utf8 -ES `"$renderer`" --config-dir `"$ClaudeConfigDir`""
$startInfo.UseShellExecute = $false
$startInfo.RedirectStandardInput = $true
$startInfo.RedirectStandardOutput = $true
$startInfo.RedirectStandardError = $true
$process = New-Object System.Diagnostics.Process
$process.StartInfo = $startInfo
[void]$process.Start()
$payloadBytes = [System.Text.Encoding]::UTF8.GetBytes($payload)
$process.StandardInput.BaseStream.Write($payloadBytes, 0, $payloadBytes.Length)
$process.StandardInput.BaseStream.Close()
$outputStream = New-Object System.IO.MemoryStream
$process.StandardOutput.BaseStream.CopyTo($outputStream)
$output = [System.Text.Encoding]::UTF8.GetString($outputStream.ToArray())
$errorOutput = $process.StandardError.ReadToEnd()
$process.WaitForExit()
$exitCode = $process.ExitCode
$process.Close()
if ($exitCode -ne 0 -or -not $output -or $output -notmatch 'Claude') {
    if ($errorOutput) {
        Write-Error $errorOutput
    }
    throw 'Status-line smoke test failed.'
}

$pythonCommand = $PythonExe.Replace('\', '/')
$rendererCommand = $renderer.Replace('\', '/')
$configCommand = $ClaudeConfigDir.Replace('\', '/')
if ($pythonCommand.Contains(' ')) {
    $pythonCommand = '"' + $pythonCommand + '"'
}
if ($rendererCommand.Contains(' ')) {
    $rendererCommand = '"' + $rendererCommand + '"'
}
if ($configCommand.Contains(' ')) {
    $configCommand = '"' + $configCommand + '"'
}

$command = "$pythonCommand -X utf8 -ES $rendererCommand --config-dir $configCommand"
$jsonCommand = $command | ConvertTo-Json -Compress

Write-Host "installed $renderer"
Write-Host ''
Write-Host "Add this to $(Join-Path $ClaudeConfigDir 'settings.json'):"
Write-Host ''
Write-Host '  "statusLine": {'
Write-Host '    "type": "command",'
Write-Host "    `"command`": $jsonCommand,"
Write-Host '    "refreshInterval": 1'
Write-Host '  }'
Write-Host ''
Write-Host 'refreshInterval is what makes the countdowns tick.'
