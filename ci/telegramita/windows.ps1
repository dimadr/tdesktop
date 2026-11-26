param([Parameter(Mandatory=$true)][ValidateSet('base','media','rtc','qt','rust','app')][string]$Group)
$ErrorActionPreference = 'Stop'
$root = 'D:\TBuild'
$source = Join-Path $root 'tdesktop'
$scratch = Join-Path $root 'scratch'
$cache = Join-Path $source 'ci\telegramita\windows_cache.py'
$minimum = @{base=45; media=65; rtc=65; qt=85; rust=35; app=90}
$env:TEMP = $scratch
$env:TMP = $scratch
$env:CARGO_INCREMENTAL = '0'
$env:SKIP_DUMPSYMS = '1'
$env:CMAKE_BUILD_PARALLEL_LEVEL = '3'
New-Item -ItemType Directory -Path $scratch -Force | Out-Null
function Free-DiskGiB {
    (Get-PSDrive -Name D).Free / 1GB
}
function Check-Disk([int]$Limit) {
    $free = Free-DiskGiB
    Write-Host ('Disk D: {0:N1} GiB free' -f $free)
    if ($free -lt $Limit) { throw "D: must have $Limit GiB free for $Group" }
}
function Invoke-Native([string[]]$Commands, [string]$Name) {
    $batch = Join-Path $scratch "$Name.cmd"
    $log = Join-Path $scratch "$Name.log"
    @('@echo off',
      ('call "' + $vs + '\VC\Auxiliary\Build\vcvarsall.bat" x64 10.0.26100.0 -vcvars_ver=14.44'),
      'if errorlevel 1 exit /b 1',
      'if not "%VCToolsVersion%"=="14.44.35207" exit /b 1',
      'if not "%WindowsSDKVersion%"=="10.0.26100.0\" exit /b 1',
      'set "QT=6.12.0"') + $Commands | Set-Content -LiteralPath $batch -Encoding ascii
    Write-Host "Starting $Name"
    $clock = [Diagnostics.Stopwatch]::StartNew()
    $lastReport = 0.0
    $cacheToken = $env:GH_TOKEN
    try {
        Remove-Item Env:GH_TOKEN -ErrorAction SilentlyContinue
        $proc = Start-Process -FilePath 'cmd.exe' -ArgumentList '/d','/c',$batch -NoNewWindow -PassThru -RedirectStandardOutput $log -RedirectStandardError "$log.stderr"
    } finally {
        $env:GH_TOKEN = $cacheToken
    }
    while (-not $proc.WaitForExit(4000)) {
        if ($clock.Elapsed.TotalSeconds - $lastReport -ge 90) {
            $lastReport = $clock.Elapsed.TotalSeconds
            Get-Content -LiteralPath $log -Tail 6 -ErrorAction SilentlyContinue | Write-Host
        }
        if ((Free-DiskGiB) -lt 12) {
            & taskkill.exe /PID $proc.Id /T /F | Out-Host
            throw "$Name stopped at the 12 GiB disk reserve"
        }
    }
    Get-Content -LiteralPath $log -Tail 180 | Write-Host
    Get-Content -LiteralPath "$log.stderr" -Tail 120 | Write-Host
    if ($proc.ExitCode -ne 0) { throw "$Name exited $($proc.ExitCode)" }
}
function Measure-Step([string]$Name, [scriptblock]$Run) {
    $clock = [Diagnostics.Stopwatch]::StartNew()
    & $Run
    $clock.Stop()
    $script:timings[$Name] = [Math]::Round($clock.Elapsed.TotalSeconds, 2)
    Write-Host "$Name seconds: $($script:timings[$Name])"
}
function Get-ProgramTree([string]$Folder) {
    Get-ChildItem -LiteralPath $Folder -Recurse -File |
        Where-Object { $_.Extension -in '.obj','.pch','.exe','.dll' } |
        ForEach-Object { '{0}|{1}|{2}' -f $_.FullName,$_.Length,$_.LastWriteTimeUtc.Ticks } |
        Sort-Object
}
function Assert-PeX64([string]$File) {
    $r = [IO.BinaryReader]::new([IO.File]::OpenRead($File))
    try {
        if ($r.ReadUInt16() -ne 0x5a4d) { throw "$File is not PE" }
        $r.BaseStream.Position = 60
        $r.BaseStream.Position = $r.ReadUInt32()
        if ($r.ReadUInt32() -ne 0x4550 -or $r.ReadUInt16() -ne 0x8664) { throw "$File is not PE x64" }
    } finally {
        $r.Dispose()
    }
}
Check-Disk $minimum[$Group]
if ($env:RUNNER_ARCH -ne 'X64') { throw "Wrong runner architecture: $env:RUNNER_ARCH" }
$vswhere = Join-Path ([Environment]::GetFolderPath('ProgramFilesX86')) 'Microsoft Visual Studio\Installer\vswhere.exe'
$vs = & $vswhere -latest -products '*' -version '[18,19)' -requires Microsoft.VisualStudio.Component.VC.14.44.17.14.x86.x64 -property installationPath
if ($LASTEXITCODE -ne 0 -or !$vs) { throw 'Required Visual Studio 2026 toolset unavailable' }
$timings = [ordered]@{}
Measure-Step 'restore' {
    $script:state = & python $cache inputs $Group
    if ($LASTEXITCODE -ne 0) { throw 'Cache restore failed' }
    $script:state | Write-Host
}
if ($Group -ne 'app') {
    if ($state -match "^CACHE HIT $Group ") { return }
    if (!(Test-Path "$source\cmake\external\angle\qt.cmake")) {
        & git -C $source submodule update --init --depth 1 cmake
        if ($LASTEXITCODE -ne 0) { throw 'CMake helper submodule checkout failed' }
    }
    Check-Disk $minimum[$Group]
    $stages = & python $cache stages $Group
    if ($LASTEXITCODE -ne 0 -or !$stages) { throw 'Could not resolve the preparation stage list' }
    $command = "python $source\Telegram\build\prepare\prepare.py reuse-stages silent $stages"
    Measure-Step 'dependencies' { Invoke-Native @($command,'exit /b %ERRORLEVEL%') "$Group-build" }
    Check-Disk 15
    Measure-Step 'upload' {
        & python $cache save $Group
        if ($LASTEXITCODE -ne 0) { throw 'Cache publication failed' }
    }
    return
}

$sourceRevision = & python $cache verify-sources
if ($LASTEXITCODE -ne 0) { throw 'Pinned submodule verification failed' }
$sourceRevision | Write-Host
if (!(Select-String -Quiet -Path "$source\Telegram\build\version" -Pattern '^AppVersionStr\s+7\.4\.6\s*$')) { throw 'Wrong Telegram version' }
$zip = Join-Path $scratch 'mieru.zip'
$mieru = Join-Path $source 'tools\mieru\3.40.0'
$uri = 'https://github.com/enfein/mieru/releases/download/v3.40.0/mieru_3.40.0_windows_amd64.zip'
& curl.exe -fLsS --retry 3 --retry-all-errors --max-time 180 -o $zip $uri
if ($LASTEXITCODE -ne 0 -or (Get-FileHash $zip).Hash -ne 'C0DFEBC250678257A61DC371E9BAE9BE6789EC455D4E78CACBDB3450A32CCE38') { throw 'Mieru ZIP verification failed' }
& python -m zipfile -e $zip $mieru
if ($LASTEXITCODE -ne 0) { throw 'Mieru ZIP extraction failed' }
$expectedMieru = 'C120FE9389E034AC2C9A405FAF58E99BF3A7A5EA9075808BE20622FC6494BF0B'
if ((Get-FileHash "$mieru\mieru.exe").Hash -ne $expectedMieru) { throw 'Mieru runtime hash mismatch' }
Assert-PeX64 "$mieru\mieru.exe"
$version = & "$mieru\mieru.exe" version
if ($LASTEXITCODE -ne 0 -or (($version -join ' ') -notmatch '\b3\.40\.0\b')) { throw 'Mieru runtime version check failed' }
Write-Host "Mieru runtime: $version"
Check-Disk $minimum[$Group]
$build = Join-Path $root 'out'
$config = "cmake -S $source -B $build -G ""Visual Studio 18 2026"" -A x64 -T ""v143,version=14.44.35207"" -DCMAKE_SYSTEM_VERSION=10.0.26100.0 -DCMAKE_CONFIGURATION_TYPES=Release -DDESKTOP_APP_DISABLE_AUTOUPDATE=ON -DTDESKTOP_API_TEST=ON"
$compile = "cmake --build $build --config Release --target Telegram --parallel 3"
Measure-Step 'configure' { Invoke-Native @($config,'exit /b %ERRORLEVEL%') 'configure' }
Measure-Step 'build' { Invoke-Native @($compile,'exit /b %ERRORLEVEL%') 'release' }
$before = Get-ProgramTree $build
if (!$before) { throw 'No build output to verify' }
Measure-Step 'noop' { Invoke-Native @($compile,'exit /b %ERRORLEVEL%') 'noop' }
$after = Get-ProgramTree $build
if (Compare-Object $before $after -SyncWindow 0) { throw 'No-op changed an object, PCH, EXE or DLL' }
$release = Join-Path $build 'Release'
$files = @('Telegram.exe','mieru.exe','modules\x64\TelegramWidgetHelper.exe','modules\x64\d3d\d3dcompiler_47.dll')
foreach ($relative in $files) { Assert-PeX64 (Join-Path $release $relative) }
if ((Get-Item "$release\Telegram.exe").VersionInfo.ProductVersion -notmatch '^7\.4\.6(?:\.0)?$') { throw 'Telegram product version mismatch' }
if ((Get-FileHash "$release\mieru.exe").Hash -ne $expectedMieru) { throw 'Packaged Mieru has a different hash' }
Invoke-Native @("dumpbin /dependents $release\Telegram.exe",'exit /b %ERRORLEVEL%') 'imports'
if (Select-String -Path "$scratch\imports.log" -Quiet -Pattern '^\s+(Qt6\S+|VCRUNTIME\S+|MSVCP\S+)\.dll\s*$') { throw 'Unexpected dynamic Qt/MSVC dependency' }

$portable = Join-Path $root 'Telegramita-7.4.6-win64-portable'
$deliver = Join-Path $root 'deliver'
New-Item -ItemType Directory -Path $portable,$deliver | Out-Null
$manifest = [ordered]@{}
Measure-Step 'package' {
    foreach ($relative in $files) {
        $dest = Join-Path $portable $relative
        New-Item -ItemType Directory -Path (Split-Path $dest) -Force | Out-Null
        Copy-Item -LiteralPath (Join-Path $release $relative) -Destination $dest
        $manifest[$relative.Replace('\','/')] = (Get-FileHash $dest).Hash
    }
    $manifest | ConvertTo-Json | Set-Content -LiteralPath "$portable\SHA256.json" -Encoding utf8
    Compress-Archive -LiteralPath $portable -DestinationPath "$deliver\Telegramita-7.4.6-win64-portable.zip" -CompressionLevel Fastest
}
$key = & python $cache key
if ($LASTEXITCODE -ne 0) { throw 'Could not determine dependency key' }
[ordered]@{
    build='PASS'; portable='PASS'; cache_restore='PASS'; noop='PASS'; runtime='NOT TESTED'
    telegramita='7.4.6'; mieru='3.40.0'; qt='6.12.0'; arch='x64'; configuration='Release'
    api='upstream test credentials'; source=$env:GITHUB_SHA
    base='4f0d56441f3ff57e8597b5c89b369a11f84007d3'; cache_key=$key; seconds=$timings
    files=$manifest; zip_sha256=(Get-FileHash "$deliver\Telegramita-7.4.6-win64-portable.zip").Hash
} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath "$deliver\report.json" -Encoding utf8
