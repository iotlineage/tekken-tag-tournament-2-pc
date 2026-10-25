$ErrorActionPreference = 'Stop'
$vswhere = Join-Path ${env:ProgramFiles(x86)} 'Microsoft Visual Studio/Installer/vswhere.exe'
$installation = & $vswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $installation) { throw 'Visual Studio C++ tools are required' }
$dev = Join-Path $installation 'Common7/Tools/VsDevCmd.bat'
$lines = & cmd.exe /s /c "`"$dev`" -no_logo -arch=x64 -host_arch=x64 >nul && set"
if ($LASTEXITCODE -ne 0) { throw 'Cannot initialize the MSVC environment' }
foreach ($line in $lines) {
    if ($line -match '^([^=]+)=(.*)$') {
        [Environment]::SetEnvironmentVariable($Matches[1], $Matches[2], 'Process')
    }
}
$env:PATH = "${env:ProgramFiles}\LLVM\bin;$env:PATH;C:\msys64\usr\bin"
$env:CI_BASH = 'C:/msys64/usr/bin/bash.exe'
python ci/.github/ci/build.py windows --source game --inputs private-inputs --output artifacts
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
