#Requires -Version 5.1
<#
.SYNOPSIS
    一键清空知识源与运营数据（保留用户与系统配置），用于从空库重新上传文档做测试。

.DESCRIPTION
    清空范围（知识源相关的业务数据）：
        documents / chunks              知识源文档与切片（含向量、BM25 词）
        jobs                            解析任务（含历史失败 / 完成记录）
        settlements                     知识沉淀（工单 / 收藏 / 手工提交）
        sessions / messages / handoffs  问答日志、追问与转人工记录
        gaps                            知识缺口
        eval_cases / eval_runs          评测用例与评测批次
        identity_pending                待认领工号（驾驶舱「可识别率」分子）
        admin_audit_log                 管理员操作审计（仅 -IncludeAuditLog 时清）
    保留（不动）：
        users / app_config / schema_migrations / captcha_challenges
        data/inbox/                     人工暂存区，不是运行期数据

    默认会先导出数据库备份，并把 data/original-docs/ 里的原件移入备份目录，
    所以"清空"是可回滚的；只有显式加 -PurgeFiles 才会真删文件。

    注意：本文件含中文，必须以 UTF-8 **带 BOM** 保存。Windows PowerShell 5.1
    在没有 BOM 时会按 ANSI（GBK）解析脚本，中文会乱码。

.PARAMETER Force
    跳过交互确认（脚本 / 自动化调用时使用）。
.PARAMETER DryRun
    只打印将清空的表与行数，不做任何修改。
.PARAMETER NoBackup
    不导出数据库备份（原件仍会移入 data/_reset-backup/<时间戳>/）。
.PARAMETER KeepFiles
    保留 data/original-docs/ 里的原件（默认与库一起收进备份目录）。
.PARAMETER PurgeFiles
    原件与 data/derived/ 直接删除，不移动（不可恢复；-DryRun 时只统计）。
.PARAMETER IncludeAuditLog
    连 admin_audit_log 一起清空（默认保留，它记录了系统配置变更）。
.PARAMETER DbContainer
    数据库容器名，默认 moqian-cbecom-db。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/reset-knowledge-data.ps1

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File scripts/reset-knowledge-data.ps1 -Force
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$DryRun,
    [switch]$NoBackup,
    [switch]$KeepFiles,
    [switch]$PurgeFiles,
    [switch]$IncludeAuditLog,
    [string]$DbContainer = 'moqian-cbecom-db'
)

$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# docker / psql 的输出统一按 UTF-8 解码，避免中文文件名与提示乱码
try {
    [Console]::OutputEncoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
    $OutputEncoding = New-Object -TypeName System.Text.UTF8Encoding -ArgumentList $false
} catch {
    # 极少数受限终端不允许改编码，忽略即可
}

$repoRoot = Split-Path -Parent $PSScriptRoot
$dataDir = Join-Path $repoRoot 'data'
$originalDir = Join-Path $dataDir 'original-docs'
$derivedDir = Join-Path $dataDir 'derived'
$envFile = Join-Path $repoRoot '.env'
$workerContainer = 'moqian-cbecom-worker'

# ---- 清空清单（与 app/migrations 对齐；新增表要同步这里） ----
$clearTables = @(
    'documents',
    'chunks',
    'jobs',
    'settlements',
    'sessions',
    'messages',
    'handoffs',
    'gaps',
    'eval_cases',
    'eval_runs',
    'identity_pending'
)
if ($IncludeAuditLog) {
    $clearTables += 'admin_audit_log'
}
$keepTables = @('users', 'app_config', 'schema_migrations', 'captcha_challenges')

function Get-DotEnvValue {
    param([string]$Key, [string]$Default)
    if (-not (Test-Path -LiteralPath $envFile)) { return $Default }
    foreach ($line in (Get-Content -LiteralPath $envFile -Encoding utf8)) {
        if ($line -match ('^\s*' + [regex]::Escape($Key) + '\s*=\s*(.*?)\s*$')) {
            $value = $Matches[1]
            if ($value) { return $value }
        }
    }
    return $Default
}

function Invoke-Docker {
    param([string[]]$DockerArgs, [switch]$AllowFailure)
    & docker @DockerArgs
    if ($LASTEXITCODE -ne 0 -and -not $AllowFailure) {
        throw "docker $($DockerArgs -join ' ') 失败（退出码 $LASTEXITCODE）"
    }
}

function Get-TableCounts {
    param([string[]]$Tables)
    $queries = @()
    foreach ($table in $Tables) {
        $queries += "select '$table' as t, count(*)::bigint as n from $table"
    }
    $sql = ($queries -join ' union all ') + ' order by t'
    $out = & docker exec $DbContainer psql -U $DbUser -d $DbName -t -A -F '|' -c $sql
    if ($LASTEXITCODE -ne 0) { throw "读取表行数失败（退出码 $LASTEXITCODE）" }
    $map = [ordered]@{}
    foreach ($line in $out) {
        if ([string]::IsNullOrWhiteSpace($line)) { continue }
        $parts = $line -split '\|'
        if ($parts.Count -ge 2) { $map[$parts[0]] = [int64]$parts[1] }
    }
    return $map
}

function Test-DbContainer {
    $running = & docker inspect -f '{{.State.Running}}' $DbContainer 2>$null
    if ($LASTEXITCODE -ne 0) {
        throw "找不到数据库容器 $DbContainer。先执行 docker compose up -d。"
    }
    if ($running -ne 'true') {
        throw "数据库容器 $DbContainer 未运行。先执行 docker compose up -d。"
    }
}

# ---------------------------------------------------------------- 前置检查
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw '未找到 docker 命令：本脚本依赖 Docker Desktop。'
}
Test-DbContainer

$DbUser = Get-DotEnvValue -Key 'POSTGRES_USER' -Default 'moqian'
$DbName = Get-DotEnvValue -Key 'POSTGRES_DB' -Default 'moqian_cbecom'

Write-Host ''
Write-Host '=== 知识源数据清空 ===' -ForegroundColor Cyan
Write-Host ("数据库容器 : {0}" -f $DbContainer)
Write-Host ("目标库     : {0}（用户 {1}）" -f $DbName, $DbUser)
Write-Host ("清空表     : {0}" -f ($clearTables -join ', '))
Write-Host ("保留表     : {0}" -f ($keepTables -join ', '))
if (-not $IncludeAuditLog) { Write-Host '另保留     : admin_audit_log（管理员操作审计；加 -IncludeAuditLog 一起清）' }
if ($DryRun) { Write-Host '模式       : DryRun（只看不改）' -ForegroundColor Yellow }

$counts = Get-TableCounts -Tables $clearTables
$totalRows = 0
foreach ($table in $clearTables) {
    $n = if ($counts.Contains($table)) { $counts[$table] } else { 0 }
    $totalRows += $n
    Write-Host ("  {0,-18} {1,8} 行" -f $table, $n)
}
Write-Host ("合计       : {0} 行" -f $totalRows)

# 提醒：出现清单外的非空表，说明新功能加了表但没纳入清空/保留清单
$known = @($clearTables + $keepTables + @('admin_audit_log'))
$unknownSql = "select relname || '|' || n_live_tup from pg_stat_user_tables where n_live_tup > 0 order by relname"
$unknownLines = & docker exec $DbContainer psql -U $DbUser -d $DbName -t -A -c $unknownSql
if ($LASTEXITCODE -ne 0) { throw "读取表清单失败（退出码 $LASTEXITCODE）" }
$unknown = @()
foreach ($line in $unknownLines) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $name = ($line -split '\|')[0]
    if ($known -notcontains $name) { $unknown += $name }
}
if ($unknown.Count -gt 0) {
    Write-Host ''
    Write-Host ("注意：以下表非空且不在清单内，本脚本不会处理（如属知识源数据请补进脚本）：{0}" -f ($unknown -join ', ')) -ForegroundColor Yellow
}

$fileCount = 0
if (Test-Path -LiteralPath $originalDir) {
    $fileCount = @(Get-ChildItem -LiteralPath $originalDir -File -Force -ErrorAction SilentlyContinue).Count
}
$derivedCount = 0
if (Test-Path -LiteralPath $derivedDir) {
    $derivedCount = @(Get-ChildItem -LiteralPath $derivedDir -Force -ErrorAction SilentlyContinue).Count
}
Write-Host ("原件       : data/original-docs/ 共 {0} 个文件（{1}）" -f $fileCount, $(if ($KeepFiles) { '保留' } elseif ($PurgeFiles) { '直接删除' } else { '移入备份目录' }))
Write-Host ("工具产物   : data/derived/ 共 {0} 项（{1}）" -f $derivedCount, $(if ($PurgeFiles) { '直接删除' } else { '移入备份目录' }))
Write-Host '不改动     : data/inbox/（人工暂存区）'

$stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$backupRoot = Join-Path $dataDir ("_reset-backup\{0}" -f $stamp)
if (-not $NoBackup) { Write-Host ("备份目录   : {0}" -f $backupRoot) }

if ($DryRun) {
    Write-Host ''
    Write-Host 'DryRun 结束：未做任何修改。' -ForegroundColor Yellow
    exit 0
}

# ---------------------------------------------------------------- 确认
if (-not $Force) {
    Write-Host ''
    Write-Host '以上数据将被清空（可回滚的前提是备份成功）。' -ForegroundColor Yellow
    $answer = (Read-Host '确认请输入 yes（其他任意输入取消）').Trim()
    if ($answer -ne 'yes') {
        Write-Host '已取消，未做任何修改。' -ForegroundColor Yellow
        exit 1
    }
}

# ---------------------------------------------------------------- 备份 + 清空
$stoppedWorker = $false
$movedFiles = 0
$removedFiles = 0
try {
    # 1) 先停 worker，避免它一边解析一边往被清空的表里写
    $workerState = & docker inspect -f '{{.State.Running}}' $workerContainer 2>$null
    if ($LASTEXITCODE -eq 0 -and $workerState -eq 'true') {
        Write-Host '停止 worker...' -NoNewline
        Invoke-Docker -DockerArgs @('stop', $workerContainer) | Out-Null
        $stoppedWorker = $true
        Write-Host ' 完成'
    }

    if (-not $NoBackup) {
        Write-Host '导出数据库备份...' -NoNewline
        New-Item -ItemType Directory -Force -Path $backupRoot | Out-Null
        $dumpTmp = "/tmp/commerce-reset-$stamp.dump"
        $dumpCmd = "pg_dump -U '$DbUser' -d '$DbName' -Fc -f '$dumpTmp'"
        Invoke-Docker -DockerArgs @('exec', $DbContainer, 'sh', '-c', $dumpCmd) | Out-Null
        Invoke-Docker -DockerArgs @('cp', ("{0}:{1}" -f $DbContainer, $dumpTmp), (Join-Path $backupRoot 'before-reset.dump')) | Out-Null
        Invoke-Docker -DockerArgs @('exec', $DbContainer, 'rm', '-f', $dumpTmp) -AllowFailure | Out-Null
        $manifest = @(
            ("time      : {0}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss')),
            ("database  : {0} (user {1})" -f $DbName, $DbUser),
            ("branch    : {0}" -f (& git -C $repoRoot rev-parse --abbrev-ref HEAD 2>$null)),
            ("commit    : {0}" -f (& git -C $repoRoot rev-parse --short HEAD 2>$null)),
            ''
            'rows before reset:'
        )
        foreach ($table in $clearTables) {
            $manifest += ("  {0,-18} {1,8}" -f $table, $(if ($counts.Contains($table)) { $counts[$table] } else { 0 }))
        }
        $manifest | Set-Content -LiteralPath (Join-Path $backupRoot 'manifest.txt') -Encoding utf8
        Write-Host ' 完成'
    }

    # 2) 文件：默认移入备份目录（与库一起回滚），-PurgeFiles 才真删
    if (-not $KeepFiles -and $fileCount -gt 0) {
        Write-Host '清理原件...' -NoNewline
        if (-not $PurgeFiles) {
            $targetDir = Join-Path $backupRoot 'original-docs'
            New-Item -ItemType Directory -Force -Path $targetDir | Out-Null
            foreach ($file in @(Get-ChildItem -LiteralPath $originalDir -File -Force)) {
                Move-Item -LiteralPath $file.FullName -Destination $targetDir -Force
                $movedFiles++
            }
        } else {
            foreach ($file in @(Get-ChildItem -LiteralPath $originalDir -File -Force)) {
                Remove-Item -LiteralPath $file.FullName -Force
                $removedFiles++
            }
        }
        Write-Host (" 完成（{0} 个）" -f $fileCount)
    }

    if ($derivedCount -gt 0) {
        Write-Host '清理 derived/...' -NoNewline
        if ($PurgeFiles) {
            foreach ($item in @(Get-ChildItem -LiteralPath $derivedDir -Force)) {
                Remove-Item -LiteralPath $item.FullName -Recurse -Force
                $removedFiles++
            }
        } else {
            $derivedTarget = Join-Path $backupRoot 'derived'
            New-Item -ItemType Directory -Force -Path $derivedTarget | Out-Null
            foreach ($item in @(Get-ChildItem -LiteralPath $derivedDir -Force)) {
                Move-Item -LiteralPath $item.FullName -Destination $derivedTarget -Force
                $movedFiles++
            }
        }
        Write-Host ' 完成'
    }

    # 3) 一个事务里清空；lock_timeout 避免卡在未结束的上传 / 问答事务上
    Write-Host '清空数据表...' -NoNewline
    $sql = "begin; set local lock_timeout = '20s'; truncate table " +
        ($clearTables -join ', ') + ' restart identity cascade; commit;'
    $truncateOut = & docker exec $DbContainer psql -U $DbUser -d $DbName -v ON_ERROR_STOP=1 -c $sql 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw ("清空失败（退出码 {0}）：{1}" -f $LASTEXITCODE, ($truncateOut -join "`n"))
    }
    Write-Host ' 完成'
} finally {
    if ($stoppedWorker) {
        Write-Host '启动 worker...' -NoNewline
        Invoke-Docker -DockerArgs @('start', $workerContainer) -AllowFailure | Out-Null
        for ($i = 0; $i -lt 20; $i++) {
            $state = & docker inspect -f '{{.State.Running}}' $workerContainer 2>$null
            if ($state -eq 'true') { break }
            Start-Sleep -Milliseconds 500
        }
        Write-Host ' 完成'
    }
}

# ---------------------------------------------------------------- 结果核对
$after = Get-TableCounts -Tables $clearTables
$left = 0
foreach ($table in $clearTables) {
    if ($after.Contains($table) -and $after[$table] -gt 0) { $left += $after[$table] }
}
$keptSql = "select 'users' as t, count(*)::bigint from users union all select 'app_config', count(*) from app_config"
$keptOut = & docker exec $DbContainer psql -U $DbUser -d $DbName -t -A -F '|' -c $keptSql
$kept = @{}
foreach ($line in $keptOut) {
    if ([string]::IsNullOrWhiteSpace($line)) { continue }
    $parts = $line -split '\|'
    $kept[$parts[0]] = $parts[1]
}

Write-Host ''
Write-Host '=== 结果 ===' -ForegroundColor Green
Write-Host ("已清空     : {0} 张表 / {1} 行（剩余 {2} 行）" -f $clearTables.Count, $totalRows, $left)
Write-Host ("保留       : users {0} 个、app_config {1} 项" -f $kept['users'], $kept['app_config'])
if ($movedFiles -gt 0) { Write-Host ("文件       : 已移入 {0}（{1} 项）" -f $backupRoot, $movedFiles) }
if ($removedFiles -gt 0) { Write-Host ("文件       : 已直接删除 {0} 项" -f $removedFiles) -ForegroundColor Yellow }
if (-not $NoBackup) { Write-Host ("备份       : {0}" -f (Join-Path $backupRoot 'before-reset.dump')) }
Write-Host ''
Write-Host '下一步：刷新后台 /admin-app/，重新上传文档即可从空数据开始测试。' -ForegroundColor Cyan

if ($left -gt 0) {
    Write-Host ("警告：仍有 {0} 行未清空，请检查上方清单外的表。" -f $left) -ForegroundColor Yellow
    exit 1
}
exit 0
