<#
    MAST2 一键打包脚本
    ==================

    自动完成：
      0. 构建 TS 前端 frontend\dist（npm run build）——mast2.spec 原样打包该目录，
         不重建；漏这步会打包出陈旧 UI → 打开 GUI 白屏（4.4.x 回归的真凶）
      1. (可选) 检测 Inno Setup 6，缺失则下载安装
      2. 清理 build\ dist\MAST\
      3. PyInstaller --onedir 打包（用 v2 venv .venv-v2-py313）
      4. 清掉 dist 里的开发者 API key 文件；api key\ 里还剩任何文件就中止
         4.8 拷入客户端集成（Claude Code 插件、Codex 配置工具、外部 agent 文档）
      5. 生成两份产物：
         - dist\MAST-v<version>-windows-x64.zip          (绿色版)
         - dist\MAST-Setup-v<version>-windows-x64.exe    (Inno Setup 安装包)

    用法（在 repo 根目录；-Version 必填）：
      .\installer\mast2_build.ps1 -Version 7.0.0
      .\installer\mast2_build.ps1 -Version 7.0.0 -SkipInstaller    # 只生成 zip
      .\installer\mast2_build.ps1 -Version 7.0.0 -SkipPyInstaller  # 复用现有 dist\MAST\（版本号须与它一致）
      .\installer\mast2_build.ps1 -Version 7.0.0 -SkipFrontend     # 不重建前端（自证 dist 最新）
      .\installer\mast2_build.ps1 -Version 7.0.0 -IncludeLiteratureIndex -IncludeVisionModels  # 带上大体积可选资产
#>

param(
    # 必填，不给缺省值。缺省值只会过期：它停在 "2.0.0" 时发布线已经到 6.x，后来又停在
    # "6.4.0" 而发布线已经过了 6.5 —— 忘了传 -Version 就打出一个版本号倒退的包，构建成功、
    # 没有任何告警，要到客户端比对更新清单时才暴露成「这个更新装不上」。
    # 也不从 _buildinfo.py 取：那是**上一次构建**写下的版本号（而且被 gitignore，干净检出里
    # 根本没有），拿它当缺省 = 两个不同的包挂同一个版本号。漏传时 PowerShell 当场询问，
    # 非交互运行直接失败。
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+(-[0-9A-Za-z.]+)?$')]
    [string]$Version,
    [switch]$SkipPyInstaller,
    [switch]$SkipInstaller,
    [switch]$SkipFrontend,
    [switch]$KeepDevKeys,
    # 默认不打包两类大体积可选资产；需要时显式开启。
    #   -IncludeLiteratureIndex  文献语义索引（MASTv2\artifacts\literature_index，约 280 MB）
    #   -IncludeVisionModels     VIGIL 视觉模型 + 质量评分器 + DINOv3 主干缓存
    # 不含时文献库与视觉功能优雅降级（视觉模型另受 legacy_features_enabled 控制）。
    [switch]$IncludeLiteratureIndex,
    [switch]$IncludeVisionModels
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent $PSScriptRoot
Set-Location $RepoRoot

function Write-Step($msg) {
    Write-Host ""
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Find-Iscc {
    $candidates = @(
        "C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        "C:\Program Files\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "${env:ProgramFiles}\Inno Setup 6\ISCC.exe"
    )
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

# 下面两个步骤写成函数，是为了让 tests/v2/unit/test_installer_7_0.py 能把它们从这份脚本里
# 原样取出来、对着临时目录真跑一遍（整份脚本在没有 PyInstaller 的机器上跑不起来）。

# Step 4.8：客户端集成拷进 dist。目录整棵拷，跳过 __pycache__ / .pytest_cache /
# .mast-fetch 和 .pyc。返回拷完之后 dist 里**仍然缺**的必需文件（相对路径）；空 = 齐了。
function Copy-ClientIntegrations([string]$RepoRoot, [string]$DistRoot) {
    # 每一项在 repo 和安装目录里的相对位置相同。
    $items = @(
        ".claude-plugin\marketplace.json",
        "integrations\claude-code\.claude-plugin",
        "integrations\claude-code\.mcp.json",
        "integrations\claude-code\server",
        "integrations\claude-code\skills",
        "integrations\claude-code\README.md",
        "integrations\claude-code\README.zh.md",
        "integrations\codex",
        "integrations\setup_clients.py",
        "docs\external"
    )
    # 少一个就让构建失败：市场清单、插件清单与 MCP 配置、桥的入口、操作规则、安装器 [Run]
    # 要调的脚本、写进 Codex 工作区的规则、外部文档。核对的是拷完之后的 dist，不是源目录。
    $required = @(
        ".claude-plugin\marketplace.json",
        "integrations\claude-code\.claude-plugin\plugin.json",
        "integrations\claude-code\.mcp.json",
        "integrations\claude-code\server\run_server.py",
        "integrations\claude-code\server\mast_mcp\tools.py",
        "integrations\claude-code\skills\mast-operator\SKILL.md",
        "integrations\codex\AGENTS.md",
        "integrations\setup_clients.py",
        "docs\external\en\README.md",
        "docs\external\zh\README.md"
    )
    # 先清掉上一次留下的：-SkipPyInstaller 复用旧 dist\MAST 时，旧版桥的文件不能混进新包。
    foreach ($old in @("integrations", ".claude-plugin", "docs\external")) {
        $p = Join-Path $DistRoot $old
        if (Test-Path -LiteralPath $p) { Remove-Item -LiteralPath $p -Recurse -Force }
    }
    $n = 0
    foreach ($rel in $items) {
        $src = Join-Path $RepoRoot $rel
        $dst = Join-Path $DistRoot $rel
        if (-not (Test-Path -LiteralPath $src)) { continue }
        if (Test-Path -LiteralPath $src -PathType Container) {
            $base = (Get-Item -LiteralPath $src).FullName.TrimEnd('\')
            $files = @(Get-ChildItem -LiteralPath $base -Recurse -File -Force | Where-Object {
                $_.FullName -notmatch '\\(__pycache__|\.pytest_cache|\.mast-fetch)\\' -and
                $_.Extension -notin @('.pyc', '.pyo')
            })
            foreach ($f in $files) {
                $to = Join-Path $dst $f.FullName.Substring($base.Length + 1)
                New-Item -ItemType Directory -Force -Path (Split-Path -Parent $to) | Out-Null
                Copy-Item -LiteralPath $f.FullName -Destination $to -Force
                $n++
            }
        } else {
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
            Copy-Item -LiteralPath $src -Destination $dst -Force
            $n++
        }
    }
    Write-Host "  拷入 $n 个文件"
    return @($required | Where-Object { -not (Test-Path -LiteralPath (Join-Path $DistRoot $_)) })
}

# Step 4：.env 清理之后 api key\ 里还剩的文件。-KeepDevKeys 只放行顶层的 .env（那个开关
# 的本意），证书、私钥照样算。不吞 Get-ChildItem 的错误：读不全就让构建失败，而不是当成空。
function Get-ApiKeyStowaways([string]$KeyDir, [switch]$AllowDevEnv) {
    if (-not (Test-Path -LiteralPath $KeyDir)) { return @() }
    $root = (Get-Item -LiteralPath $KeyDir).FullName.TrimEnd('\')
    $files = @(Get-ChildItem -LiteralPath $root -Recurse -File -Force)
    if ($AllowDevEnv) {
        $files = @($files | Where-Object { -not ($_.Extension -eq '.env' -and $_.DirectoryName -eq $root) })
    }
    return $files
}

# ─── Pre-flight ──────────────────────────────────────────────────────

if (-not (Test-Path "mast2.spec")) {
    Write-Error "在 $RepoRoot 没有找到 mast2.spec。"
    exit 1
}
$pyexe = Join-Path $RepoRoot ".venv-v2-py313\Scripts\python.exe"
if (-not (Test-Path $pyexe)) {
    Write-Error "找不到 .venv-v2-py313\Scripts\python.exe。请先创建 v2 虚拟环境。"
    exit 1
}

Write-Host "MAST 打包 v$Version" -ForegroundColor Green
Write-Host "Repo: $RepoRoot"
Write-Host "Python: $pyexe"

# ─── Inno Setup detect ───────────────────────────────────────────────

$iscc = $null
if (-not $SkipInstaller) {
    $iscc = Find-Iscc
    if (-not $iscc) {
        Write-Warning "Inno Setup 6 未安装 — 只生成 zip。"
        $SkipInstaller = $true
    } else {
        Write-Host "Inno Setup 6 OK: $iscc"
    }
}

# ─── Step 0: rebuild the TS SPA (frontend/dist) ──────────────────────
# mast2.spec bundles frontend/dist AS-IS — it does NOT rebuild it. If that tree
# is stale, the installer ships a stale/mismatched UI and the browser opens to a
# BLANK PAGE (the 4.4.x 打开GUI空白 regression; root-caused 2026-07-03: the
# release never rebuilt the SPA, so an old dist from before the design rewrite
# got packaged). Always rebuild from source before packaging so the shipped UI
# matches the current code + API schema. -SkipFrontend reuses an existing dist.
if ((-not $SkipPyInstaller) -and (-not $SkipFrontend)) {
    Write-Step "构建 TS 前端 frontend\dist (npm run build — tsc + vite)"
    $npm = Get-Command npm -ErrorAction SilentlyContinue
    if (-not $npm) {
        Write-Error "找不到 npm（Node.js 未安装或不在 PATH）。装 Node.js，或用 -SkipFrontend（须自行确认 frontend\dist 已是最新，否则会打包出白屏 UI）。"
        exit 1
    }
    if (-not (Test-Path "frontend\node_modules")) {
        Write-Host "  frontend\node_modules 缺失 — 先跑 npm ci"
        Push-Location frontend
        try { & $npm.Source ci } finally { Pop-Location }
        if ($LASTEXITCODE -ne 0) { Write-Error "npm ci 失败（退出码 $LASTEXITCODE）。"; exit 1 }
    }
    Push-Location frontend
    try { & $npm.Source run build } finally { Pop-Location }
    if ($LASTEXITCODE -ne 0) {
        Write-Error "前端构建失败（退出码 $LASTEXITCODE）。拒绝打包陈旧 UI。"
        exit 1
    }
    $spaIndex = "frontend\dist\index.html"
    if ((-not (Test-Path $spaIndex)) -or (-not (Test-Path "frontend\dist\assets"))) {
        Write-Error "前端构建完成但 $spaIndex / assets 不存在。"
        exit 1
    }
    # 一致性自检：index.html 引用的每个 /assets/*.js|css 必须真实存在。这正是
    # 白屏的直接成因——hash 不匹配 → 资源 404 → #root 永不挂载。
    $idxHtml = Get-Content $spaIndex -Raw
    $refs = [regex]::Matches($idxHtml, '/assets/[A-Za-z0-9._-]+\.(?:js|css)') | ForEach-Object { $_.Value } | Select-Object -Unique
    foreach ($ref in $refs) {
        $refPath = Join-Path "frontend\dist" ($ref -replace '^/', '')
        if (-not (Test-Path $refPath)) {
            Write-Error "前端产物不一致：index.html 引用 $ref 但该文件不存在（会导致白屏）。"
            exit 1
        }
    }
    Write-Host "  前端构建完成；index.html 的 $($refs.Count) 个资产引用已全部核对存在"
} elseif ($SkipFrontend) {
    Write-Step "跳过前端构建（-SkipFrontend）— 复用现有 frontend\dist"
    if (-not (Test-Path "frontend\dist\index.html")) {
        Write-Error "frontend\dist\index.html 不存在；-SkipFrontend 无法复用。请去掉该开关以重新构建。"
        exit 1
    }
    Write-Warning "  未重建前端 — 请自行确认 frontend\dist 与当前源码一致（否则可能白屏）。"
}

# ─── Step 1: clean old build ─────────────────────────────────────────

if (-not $SkipPyInstaller) {
    # ─── 先把上一版的 onedir 存成基线，再清理 ──────────────────────────
    #
    # 这一步 2026-08-13 才加。在此之前这里直接 Remove-Item dist\MAST ——
    # 而 dist\MAST 正是**下一次做增量包所必需的旧树**。
    #
    # 代价是真实发生过的：6.2.13 要发的时候，机子上装着 6.2.12，而本地只剩
    # 6.2.3 的基线（更近的基线每个约 5 GB，早被手工删了）。5.04 GB / 27285
    # 个文件从机子拉回来不现实，本机也没有 innoextract 能从安装包里解出旧树，
    # 于是只能让机子算 27250 个文件的哈希清单传回来、再手工拼一个增量包。
    # 那是一小时的绕路，而它本来只需要这里一次 Move-Item。
    #
    # 用**上一版自己报的版本号**命名（读 _buildinfo.py，此刻还没被下一步覆写），
    # 而不是用时间戳或 "prev"：一个叫 prev 的目录在第二次构建之后就说不清它是
    # 哪一版，而增量包的 from_version 必须写得出来。
    $prevBase = $null
    if (Test-Path "dist\MAST") {
        # 版本号**先问这棵树自己**，读不到再退回源码里的 _buildinfo.py。
        #
        # 2026-08-24：原来只问 _buildinfo.py —— 而那是**源码此刻**的版本号，
        # 不是 dist\MAST 被构建出来时的版本号。两者之间隔着任何一次手工改版本、
        # 或一次半途失败的构建，这份基线就会被归到**错的版本名**下，
        # 而增量包的 from_version 会照着那个错名字写出来，一路传到机器上。
        # **名字必须和树同源。**
        $prevVer = $null
        $stamp = "dist\MAST\_internal\docs\release_notes_current.txt"
        if (Test-Path $stamp) {
            $line = Get-Content -LiteralPath $stamp -TotalCount 1
            if ($line -match 'v(\d+\.\d+\.\d+)') { $prevVer = $Matches[1] }
        }
        if ((-not $prevVer) -and (Test-Path "MASTv2\mast\_buildinfo.py")) {
            $m = Select-String -Path "MASTv2\mast\_buildinfo.py" `
                               -Pattern "VERSION\s*=\s*'([^']+)'" | Select-Object -First 1
            if ($m) {
                $prevVer = $m.Matches[0].Groups[1].Value
                Write-Warning "  dist\MAST 里没有版本戳，退回 _buildinfo.py 的 v$prevVer —— 它未必是这棵树的版本"
            }
        }
        if ($prevVer) {
            $prevBase = "dist\MAST_" + ($prevVer -replace '\.', '') + "_base"
            Write-Step "保留上一版 onedir 作为增量基线: dist\MAST -> $prevBase"
            if (Test-Path $prevBase) {
                # **覆盖基线要出声。** 原来这里是一句静默的 Remove-Item：
                # 同一个版本号构建两次，第二次会无声抹掉第一次的基线 —— 而基线是
                # 「机器上装的到底是哪一版」的**唯一实物证据**。2026-08-23 那份
                # 陈旧的 6.3.7 基线就是这么消失的，全程没有一行输出提到过它。
                # 手工建的 MAST_637i_base 之所以活下来，纯粹因为脚本拼不出那个名字。
                $oldExe = Join-Path $prevBase "MAST.exe"
                $h = "?"
                if (Test-Path $oldExe) {
                    $h = (Get-FileHash -Algorithm SHA256 -LiteralPath $oldExe).Hash.Substring(0, 8)
                }
                Write-Warning "  $prevBase 已存在（MAST.exe $h...），本次将被覆盖。"
                Write-Warning "  要留住它就先改名 —— 脚本只认 MAST_<版本去点>_base 这个形状，别的名字它碰不到。"
                Remove-Item -Recurse -Force $prevBase
            }
            Move-Item -LiteralPath "dist\MAST" -Destination $prevBase -Force
            Write-Host "  已保留 v$prevVer 基线（下次 python -m mast.update delta 用它当 old）"
        } else {
            # 读不到版本号就**不保留** —— 一个说不清是哪一版的基线，比没有基线坏：
            # 它会被当成某个版本用掉，而增量包的 from_version 就成了一句假话。
            Write-Warning "  读不到上一版版本号，dist\MAST 直接删除（无法命名基线）"
        }
    }

    Write-Step "清理 build\ dist\MAST\"
    if (Test-Path "build")     { Remove-Item -Recurse -Force "build" }
    if (Test-Path "dist\MAST") { Remove-Item -Recurse -Force "dist\MAST" }
    Write-Host "  完成"
}

# ─── Step 2: refresh build info ──────────────────────────────────────

if (-not $SkipPyInstaller) {
    Write-Step "刷新 MASTv2\mast\_buildinfo.py = $Version + 当前时间"
    $stamp = (Get-Date).ToString("yyyy-MM-ddTHH:mm:sszzz")
    $bi = "`"`"`"Auto-generated by installer/mast2_build.ps1. DO NOT EDIT.`"`"`"`r`n" +
          "VERSION = '$Version'`r`n" +
          "RELEASED_AT = '$stamp'`r`n"
    Set-Content -Path "MASTv2\mast\_buildinfo.py" -Value $bi -Encoding UTF8 -NoNewline
    Write-Host "  写入 MASTv2\mast\_buildinfo.py : VERSION=$Version  RELEASED_AT=$stamp"

    # ─── Step 2.5: generate current-version release notes (.txt) ──────────
    # The launcher's "当前版本更新说明" opens docs/release_notes_current.txt.
    # Auto-build it from the human-edited seed body docs/release_notes_body.txt
    # so every package ships an up-to-date, version-stamped notes file. Both a
    # "current" alias and a versioned copy are written (UTF-8, no BOM) and the
    # mast2.spec docs bundler ships both into the frozen docs/ dir.
    Write-Step "生成本版本更新说明 docs\release_notes_current.txt + release_notes_v$Version.txt"
    $notesBody = if (Test-Path "docs\release_notes_body.txt") {
        Get-Content "docs\release_notes_body.txt" -Raw
    } else {
        "(本版本未填写更新说明。)"
    }
    $notesDate = (Get-Date).ToString('yyyy-MM-dd')
    $notes = "MAST v$Version 更新说明`r`n发布日期: " + $notesDate + "`r`n" + ('=' * 40) + "`r`n`r`n" + $notesBody
    $utf8NoBom = New-Object System.Text.UTF8Encoding $false
    $notesCurrent = Join-Path $RepoRoot "docs\release_notes_current.txt"
    $notesVersioned = Join-Path $RepoRoot "docs\release_notes_v$Version.txt"
    [IO.File]::WriteAllText($notesCurrent, $notes, $utf8NoBom)
    [IO.File]::WriteAllText($notesVersioned, $notes, $utf8NoBom)
    Write-Host "  写入 docs\release_notes_current.txt + docs\release_notes_v$Version.txt (UTF-8 no BOM, 发布日期 $notesDate)"

    Write-Step "刷新 push 默认值 (MASTv2\mast\update\_defaults.py)"
    # Reuse v1's installer/push_defaults.json so a single admin machine can
    # publish both MAST and MAST2 from the same shared JSON. If the user
    # wants MAST2 on a different push server, drop a separate
    # installer/push_defaults_mast2.json and the build picks that up first.
    $pushDefaultsJson = if (Test-Path "installer\push_defaults_mast2.json") {
        "installer\push_defaults_mast2.json"
    } else {
        "installer\push_defaults.json"
    }
    $pushDefaultsPy = "MASTv2\mast\update\_defaults.py"
    if (Test-Path $pushDefaultsJson) {
        $cfg = Get-Content $pushDefaultsJson -Raw | ConvertFrom-Json
        $url = $cfg.server_url
        $tok = $cfg.token
        # Ed25519 release public key (hex) — baked in so clients VERIFY the OTA
        # manifest signature (see mast.update.signing). Empty until the admin runs
        # `python -m mast.update keygen` and pastes signing_pubkey into the JSON.
        $pub = if ($cfg.PSObject.Properties.Name -contains 'signing_pubkey') { $cfg.signing_pubkey } else { '' }
        $body = "`"`"`"Auto-generated by installer/mast2_build.ps1. DO NOT EDIT or commit.`"`"`"`r`n" +
                "DEFAULT_SERVER_URL = '$url'`r`n" +
                "DEFAULT_TOKEN = '$tok'`r`n" +
                "DEFAULT_SIGNING_PUBKEY = '$pub'`r`n"
        Set-Content -Path $pushDefaultsPy -Value $body -Encoding UTF8 -NoNewline
        $tokPreview = if ($tok.Length -ge 8) { $tok.Substring(0,8) + "..." } else { "(short)" }
        $pubState = if ($pub) { "SET(" + $pub.Substring(0,[Math]::Min(8,$pub.Length)) + "...)" } else { "EMPTY(未签名验证)" }
        Write-Host "  写入 $pushDefaultsPy : URL=$url  TOKEN=$tokPreview  SIGNING_PUBKEY=$pubState (来自 $pushDefaultsJson)"
    } else {
        Write-Warning "  $pushDefaultsJson 不存在 — bundle 将以空默认值发布，用户需要手动配置 update_server_url.env / update_client_token.env"
    }
}

# ─── Step 3: PyInstaller ─────────────────────────────────────────────

if (-not $SkipPyInstaller) {
    Write-Step "PyInstaller --onedir (5–15 分钟，请耐心等待)"
    # Run from MASTv2/ so `import mast` resolves to v2's MASTv2/mast (pathex).
    # (Historically v1's mast/ at the repo root shadowed it and had to be hidden
    # during the build; v1 was archived 2026-06-01 so no shadowing dir remains.)
    Push-Location MASTv2
    try {
        & $pyexe -m PyInstaller ..\mast2.spec --noconfirm --clean --distpath ..\dist --workpath ..\build
    } finally {
        Pop-Location
    }
    $pyiExitCode = $LASTEXITCODE
    if ($pyiExitCode -ne 0) {
        Write-Error "PyInstaller 失败（退出码 $pyiExitCode）。"
        exit 1
    }
    if (-not (Test-Path "dist\MAST\MAST.exe")) {
        Write-Error "PyInstaller 完成但 dist\MAST\MAST.exe 不存在。"
        exit 1
    }
    Write-Host "  完成: dist\MAST\MAST.exe"
} else {
    Write-Step "跳过 PyInstaller（-SkipPyInstaller）"
    if (-not (Test-Path "dist\MAST\MAST.exe")) {
        Write-Error "dist\MAST\MAST.exe 不存在；不能跳过构建。"
        exit 1
    }
    # 安装包的版本号必须是里面那棵树的版本号。-SkipPyInstaller 不重写 _buildinfo.py，
    # 这里的 -Version 只决定安装包 / zip 的文件名和 Inno 的 AppVersion —— 传错了就是一个
    # 名字写着 7.0.1、里面跑着 7.0.0 的安装包。用树自己的版本戳（Step 2.5 写的那行）核一遍。
    $treeStamp = "dist\MAST\_internal\docs\release_notes_current.txt"
    $treeVer = $null
    if (Test-Path $treeStamp) {
        $line = Get-Content -LiteralPath $treeStamp -TotalCount 1
        if ($line -match '^MAST v(\S+)') { $treeVer = $Matches[1] }
    }
    if (-not $treeVer) {
        Write-Warning "  dist\MAST 里没有版本戳 ($treeStamp)，无法核对 -Version $Version 是否就是这棵树的版本"
    } elseif ($treeVer -ne $Version) {
        Write-Error "-Version $Version 与 dist\MAST 的版本戳 v$treeVer 不一致。-SkipPyInstaller 不会重新构建，安装包会挂着错的版本号发出去。请传 -Version $treeVer，或去掉 -SkipPyInstaller 重新构建。"
        exit 1
    }
}

# ─── Step 4: clean dev keys ──────────────────────────────────────────

Write-Step "清理打包进 dist\MAST 的开发者 API key"
$keyDir = "dist\MAST\api key"
if (Test-Path $keyDir) {
    if ($KeepDevKeys) {
        Write-Warning "  -KeepDevKeys 已设：保留 .env 文件"
    } else {
        Get-ChildItem -Path $keyDir -Filter *.env -ErrorAction SilentlyContinue | ForEach-Object {
            Write-Host "  删除 $($_.FullName)"
            Remove-Item $_.FullName -Force
        }
    }
}

# .env 删完之后，api key\ 里**剩下的任何文件**都不许随包发出去。上面那句只认 *.env，
# 而这个目录里还会有 LAN 证书和它的**私钥**（lan_cert.pem / lan_key.pem，启动器在 LAN
# 模式下自动生成）、按机器钉的 update_server_ca.pem。
#
# 它们怎么进来：PyInstaller 不往这里放任何东西，但在 dist\MAST 里直接跑一次 MAST.exe
# （构建手册第 7 步的冒烟测试就是这么跑的）会把 dist\MAST 当成数据根，建出 api key\；
# 冒烟时开过 LAN 模式就有了证书和私钥，填过 key 就有了 .env。之后再用 -SkipPyInstaller
# 复用这棵树出安装包，它们就随 [Files] 的通配装到每一台机器上 —— 每台仪器电脑共用同一把
# TLS 私钥。
#
# 选「中止」而不是「全删」：删的是构建产物里的文件，但路径一旦解析错就是删真凭据；
# 中止只会多一次红构建，并把要删什么、怎么删写清楚。与 Step 5.9 管覆写目录同一个做法。
$keyStowaways = @(Get-ApiKeyStowaways -KeyDir (Join-Path $RepoRoot $keyDir) -AllowDevEnv:$KeepDevKeys)
if ($keyStowaways.Count -gt 0) {
    Write-Host ""
    Write-Error @"
dist\MAST\api key\ 里还有 $($keyStowaways.Count) 个文件:
$($keyStowaways.ForEach({ "  " + $_.FullName }) -join "`n")

api key\ 是操作员的凭据目录 (LAN 证书私钥、LAN 口令、更新令牌等)。这些文件会随安装包和
绿色版 zip 装到每一台机器上。它们通常来自在 dist\MAST 里直接跑过的冒烟测试。
删掉它们再重跑构建:
    Remove-Item -Recurse -Force "$(Join-Path $RepoRoot $keyDir)\*"
"@
    exit 1
}

# ─── Step 4.5: bundle the literature semantic index INTO the package ──
# The ~205 MB index (vectors.npy + metadata.parquet + classified.parquet) is
# gitignored and NOT in the PYZ; copy it into dist\MAST\MASTv2\artifacts so the
# Inno installer (recursesubdirs of dist\MAST) + the green zip both ship it, and
# the frozen app finds it at <exe-dir>\MASTv2\artifacts\literature_index
# (literature_index._index_base in frozen mode = the executable's dir). Result:
# a FRESH install has semantic literature search out of the box, no out-of-band
# provisioning step.

if ($IncludeLiteratureIndex) {
    Write-Step "打包文献语义索引到 dist\MAST\MASTv2\artifacts\literature_index"
    $idxSrc = Join-Path $RepoRoot "MASTv2\artifacts\literature_index"
    $idxDst = Join-Path $RepoRoot "dist\MAST\MASTv2\artifacts\literature_index"
    if (Test-Path (Join-Path $idxSrc "vectors.npy")) {
        New-Item -ItemType Directory -Force -Path $idxDst | Out-Null
        $idxTotal = 0
        foreach ($f in @("vectors.npy", "metadata.parquet", "classified.parquet", "abstracts.parquet", "manifest.json")) {
            $s = Join-Path $idxSrc $f
            if (Test-Path $s) {
                Copy-Item $s (Join-Path $idxDst $f) -Force
                $sz = [math]::Round((Get-Item $s).Length / 1MB, 1)
                $idxTotal += $sz
                Write-Host "  + $f ($sz MB)"
            }
        }
        Write-Host "  文献索引已打包（$idxTotal MB；全新安装即自带跨语言语义检索）"
    } else {
        Write-Warning "  未找到 $idxSrc\vectors.npy — 安装包将不含文献索引（搜索会优雅降级，需后续 provision）"
    }
} else {
    Write-Step "跳过文献语义索引（默认不打包；需要时加 -IncludeLiteratureIndex）"
}

# ─── Step 4.6: bundle the VIGIL v2.5 vision model (ckpt + DINOv3 backbone) ──
# The authoritative ssl_sf09c1 model: DINOv3-ViT-S/16 + LoRA(qkv_o_mlp,r8) + 6
# heads (q/c/t/n/s/k). The ckpt (~29 MB, LoRA + heads + scale-emb, NO backbone)
# and the frozen vits16 backbone weights cache (~165 MB, HF-hub layout) are
# gitignored and NOT in the PYZ. Copy them into dist\MAST\MASTv2\artifacts so
# the frozen app finds them at <exe-dir>\MASTv2\artifacts\{mast_vision_v25.pt,
# vision_backbone} (= project_root()/MASTv2/artifacts in frozen mode) and timm
# loads the backbone fully offline (HF_HOME → bundled cache, HF_HUB_OFFLINE=1;
# set by mast.vision._vigil.v25.configure_backbone_cache). Absent → the vision
# module fail-safes to legacy/mock, so a code-only build still runs.
# NOTE: v2.5 uses vits16 (small, ~165 MB) — the old M12 L/16 cache (~1.2 GB) is
# no longer bundled (huge size saving).

if ($IncludeVisionModels) {
    Write-Step "打包 VIGIL v2.5 视觉模型 (ckpt + DINOv3-vits16 主干缓存) 到 dist\MAST\MASTv2\artifacts"
    $artSrc = Join-Path $RepoRoot "MASTv2\artifacts"
    $artDst = Join-Path $RepoRoot "dist\MAST\MASTv2\artifacts"
    $v25Ckpt = Join-Path $artSrc "mast_vision_v25.pt"
    $bbSrc = Join-Path $artSrc "vision_backbone"
    if (Test-Path $v25Ckpt) {
        New-Item -ItemType Directory -Force -Path $artDst | Out-Null
        Copy-Item $v25Ckpt (Join-Path $artDst "mast_vision_v25.pt") -Force
        $ckptMB = [math]::Round((Get-Item $v25Ckpt).Length / 1MB, 1)
        Write-Host "  + mast_vision_v25.pt ($ckptMB MB)"
        # stm_quality_v1 — the learned real-data quality scorer:
        # a tiny (~23 KB) StandardScaler+Ridge head that REUSES the same vision_backbone
        # below. Frozen app finds it at <exe-dir>\MASTv2\artifacts (quality_model._resolve).
        # Absent → VisionModule.assess_quality / assess(use_learned) degrade gracefully.
        $qmSrc = Join-Path $artSrc "stm_quality_v1_dino.joblib"
        if (Test-Path $qmSrc) {
            Copy-Item $qmSrc (Join-Path $artDst "stm_quality_v1_dino.joblib") -Force
            Write-Host "  + stm_quality_v1_dino.joblib (学习质量评分器 Spearman 0.68, 复用同一主干)"
        } else {
            Write-Warning "  未找到 $qmSrc — stm_quality_v1 学习评分器缺失(assess_quality 优雅降级)"
        }
        if (Test-Path (Join-Path $bbSrc "hub")) {
            $bbDst = Join-Path $artDst "vision_backbone"
            if (Test-Path $bbDst) { Remove-Item -Recurse -Force $bbDst }
            Copy-Item $bbSrc $bbDst -Recurse -Force
            $bbMB = [math]::Round(((Get-ChildItem -Recurse -File $bbSrc | Measure-Object -Property Length -Sum).Sum) / 1MB, 0)
            Write-Host "  + vision_backbone\ ($bbMB MB, DINOv3-ViT-S/16, 离线)"
            Write-Host "  VIGIL v2.5 视觉模型已打包（6 头 q/c/t/n/s/k，全新安装即权威视觉推理，全离线）"
        } else {
            Write-Warning "  未找到 $bbSrc\hub — 主干缺失;视觉将 fail-safe 到 legacy/mock"
        }
    } else {
        Write-Warning "  未找到 $v25Ckpt — 安装包不含 VIGIL v2.5 视觉模型（视觉 fail-safe 到 legacy/mock）"
    }
} else {
    Write-Step "跳过视觉模型（默认不打包；需要时加 -IncludeVisionModels）"
}

# ─── Step 4.7: bundle the DP analysis runtime (embeddable CPython 3.13) ──
# The data-processing agent writes and runs Python. It CANNOT run inside the
# frozen bundle: PyInstaller onedir compiles pure-Python sources into the PYZ
# inside MAST.exe, so _internal\numpy\__init__.py does not exist. And putting
# _internal on the child's sys.path would be worse — _internal\nanonis_spm\
# __init__.py IS a real file, which would break the one mechanism behind
# "the DP agent cannot reach the instrument" (it has no SafetyGate, no HITL).
#
# So a separate interpreter is not the tidier option, it is the only option.
# Built by MASTv2\scripts\build_pyruntime.py; ~427 MB / 13884 files. It lands
# under MASTv2\ which update\delta.py excludes wholesale — so it ships ONLY in
# the full installer and is never re-pushed through an OTA delta. Same treatment
# as the DINOv3 backbone; a pinned interpreter + pinned wheels is static.
#
# DELIBERATE ASYMMETRY (same logic as mast2.spec's _OPTIONAL_DATA guard):
#   missing  -> Write-Warning. The app degrades honestly: find_runtime() returns
#               None and py_run reports which locations it checked.
#   BROKEN   -> throw. Shipping a runtime that looks present but cannot import
#               numpy (or worse, CAN import nanonis_spm) produces a confusing
#               failure on the operator's machine. Failing here costs one red
#               build.

Write-Step "打包 DP 分析运行时 (embeddable CPython 3.13 + 科学栈) 到 dist\MAST\MASTv2\pyruntime"
$rtSrc = Join-Path $RepoRoot "MASTv2\pyruntime"
$rtDst = Join-Path $RepoRoot "dist\MAST\MASTv2\pyruntime"
$rtExe = Join-Path $rtSrc "python.exe"
if (Test-Path $rtExe) {
    # 出厂前先验一次源目录 —— 拷 427 MB 之前就该知道它是不是坏的
    Write-Host "  自检源运行时..."
    $venvPy = Join-Path $RepoRoot ".venv-v2-py313\Scripts\python.exe"
    & $venvPy (Join-Path $RepoRoot "MASTv2\scripts\build_pyruntime.py") --selftest-only
    if ($LASTEXITCODE -ne 0) {
        throw "MASTv2\pyruntime 自检不通过（见上）。带一个坏运行时出厂，会在操作员机器上产生困惑的失败；请先修好或删掉它重建。"
    }
    if (Test-Path $rtDst) { Remove-Item -Recurse -Force $rtDst }
    Copy-Item $rtSrc $rtDst -Recurse -Force
    $rtMB = [math]::Round(((Get-ChildItem -Recurse -File $rtDst | Measure-Object -Property Length -Sum).Sum) / 1MB, 0)
    $rtN = (Get-ChildItem -Recurse -File $rtDst | Measure-Object).Count
    # 拷贝本身也会静默少东西（长路径、占用中的文件）—— 数一遍
    if ($rtN -lt 10000) {
        throw "分析运行时只拷过去 $rtN 个文件（源目录约 13884 个）—— 拷贝不完整，安装包会带一个残缺的运行时。"
    }
    $scPath = Join-Path $rtDst "Lib\site-packages\sitecustomize.py"
    if (-not (Test-Path $scPath)) {
        throw "分析运行时里没有 Lib\site-packages\sitecustomize.py —— 没有它，子进程的审计钩子不会安装，B2（不能覆盖已存在的测量文件）就没有任何机制。请重跑 build_pyruntime.py。"
    }
    Write-Host "  + pyruntime\ ($rtMB MB, $rtN 个文件, numpy/scipy/matplotlib/pandas/skimage/sklearn)"
    Write-Host "  DP 分析运行时已打包（子进程里没有 nanonis_spm 也没有 mast —— B1 由此成立）"
} else {
    Write-Warning "  未找到 $rtExe — 安装包将不含 DP 分析运行时。py_run 会如实报告「查了哪些位置都没有」并拒绝执行（不会静默降级）。要构建：.venv-v2-py313\Scripts\python.exe MASTv2\scripts\build_pyruntime.py"
}

# ─── Step 4.8: 客户端集成（Claude Code 插件 / Codex 配置工具 / 外部 agent 文档）────
# 7.0 起外部 agent（Codex 为主、Claude Code 为辅）是正式的操作入口，而 6.5.0 的安装包里
# 一个集成文件都没有 —— 组员得另找一份源码检出。现在随安装目录分发：
#   <安装目录>\.claude-plugin\marketplace.json   /plugin marketplace add <安装目录>
#   <安装目录>\integrations\claude-code\         插件本体（MCP 桥 + skills + .mcp.json）
#   <安装目录>\integrations\codex\               AGENTS.md / README / 示例配置
#   <安装目录>\integrations\setup_clients.py      安装器 [Run] 用它改插件的 python；
#                                                 Codex 用户用它写 config.toml
#   <安装目录>\docs\external\                     外部 agent 文档
# 都在 [Files] 的 dist\MAST\* 通配之内；升级时由 [InstallDelete] 先清掉旧的。
# 缺必需文件就让构建失败：一个带着空插件的安装包，要到组员第一次连仪器才会暴露。
Write-Step "打包客户端集成到 dist\MAST (Claude Code 插件 / Codex 配置工具 / 外部 agent 文档)"
$ciMissing = @(Copy-ClientIntegrations -RepoRoot $RepoRoot -DistRoot (Join-Path $RepoRoot "dist\MAST"))
if ($ciMissing.Count -gt 0) {
    Write-Error ("客户端集成缺必需文件 (拷完之后 dist\MAST 里没有):`n  " + ($ciMissing -join "`n  ") +
                 "`n在 repo 里补齐 (git status 看看是否没检出)，再重跑构建。")
    exit 1
}
Write-Host "  客户端集成已打包 (安装器会把插件的 python 指向安装目录里的随包运行时)"

# ─── Step 5: zip ─────────────────────────────────────────────────────

Write-Step "生成绿色版 zip"
$zipPath = "dist\MAST-v$Version-windows-x64.zip"
# With the M12 backbone bundled the onedir is ~2.5 GB; Compress-Archive chokes
# on payloads that large (slow + memory-heavy). The Inno installer is the
# offline artifact for big bundles — skip the green zip past a size threshold.
$distBytes = (Get-ChildItem -Recurse -File "dist\MAST" | Measure-Object -Property Length -Sum).Sum
$distGB = [math]::Round($distBytes / 1GB, 2)
if ($distBytes -gt 1.5GB) {
    Write-Warning "  dist\MAST = $distGB GB (含 M12 主干) — 跳过绿色版 zip;安装包(Inno)即全离线交付物。"
    Write-Warning "  如需 zip,用 -SkipPyInstaller 在不含主干的 dist 上单独压缩,或手动压缩。"
} else {
    if (Test-Path $zipPath) { Remove-Item $zipPath -Force }
    Compress-Archive -Path "dist\MAST\*" -DestinationPath $zipPath -CompressionLevel Optimal
    $zipSize = [math]::Round((Get-Item $zipPath).Length / 1MB, 1)
    Write-Host "  完成: $zipPath ($zipSize MB)"
}

# ─── Step 5.9: 打包前闸门 — 产物里绝不能夹带管理员覆写 ────────────────
#
# 覆写目录在冻结版里解析成 <安装目录>\config\overrides\（override_store.py:33-34,
# parents[3]）。安装器用 ignoreversion 覆盖拷贝，所以**产物里只要有一个同名 json,
# 每台机器的管理员覆写都会在升级时被静默重置** —— 包括安全包络（XY/Z 行程、偏压、
# 电流上限）。操作员那边不会有任何提示,下一次扫描才会撞上一个自己没改过的限值。
#
# 这不是假想：`dist\MAST\config\overrides\` 会被**构建手册要求的那次冒烟测试**
# 亲手创建 —— 从 dist\MAST 直接跑 MAST.exe,_DEFAULT_DIR 就落在产物里面。目前它是
# 空的所以无害,但只要冒烟时点过一次"保存覆写",或将来某版在首启写一份默认覆写,
# 就会随包发出去。
#
# 目录本身留着无妨（安装器需要它存在且可写）;有文件才是问题。
$ovrDir = Join-Path $RepoRoot "dist\MAST\config\overrides"
if (Test-Path $ovrDir) {
    $stowaways = @(Get-ChildItem -Path $ovrDir -Recurse -File -ErrorAction SilentlyContinue)
    if ($stowaways.Count -gt 0) {
        Write-Host ""
        Write-Error @"
打包产物里夹带了 $($stowaways.Count) 个管理员覆写文件:
$($stowaways.ForEach({ "  " + $_.FullName }) -join "`n")

这些文件会随安装包发出去,并在每台机器升级时覆盖掉操作员自己的覆写(含安全限值)。
删掉它们再重跑构建:
    Remove-Item -Recurse -Force "$ovrDir\*"
"@
        exit 1
    }
}

# ─── Step 6: Inno Setup ──────────────────────────────────────────────

if (-not $SkipInstaller) {
    Write-Step "生成 Inno Setup 安装包 (1–3 分钟)"
    & $iscc "/Q" "/DMAST_VERSION=$Version" "installer\mast2_setup.iss"
    if ($LASTEXITCODE -ne 0) {
        Write-Error "Inno Setup 失败（退出码 $LASTEXITCODE）。"
        exit 1
    }
    $setupExe = "dist\MAST-Setup-v$Version-windows-x64.exe"
    if (Test-Path $setupExe) {
        $setupSize = [math]::Round((Get-Item $setupExe).Length / 1MB, 1)
        Write-Host "  完成: $setupExe ($setupSize MB)"
    }

    # Standalone legacy remover for old MAST(v1)/MAST2(v2) — incremental updates
    # mean the full installer isn't re-run each release, so a separate uninstaller
    # is shipped (removes program files, preserves user data). Tiny + fast.
    Write-Step "生成旧版卸载工具 (MAST-Legacy-Uninstall.exe)"
    & $iscc "/Q" "installer\mast_legacy_uninstall.iss"
    if ($LASTEXITCODE -ne 0) {
        Write-Warning "  旧版卸载工具生成失败（退出码 $LASTEXITCODE）— 主安装包已就绪，可单独重试。"
    } elseif (Test-Path "dist\MAST-Legacy-Uninstall.exe") {
        $unSize = [math]::Round((Get-Item "dist\MAST-Legacy-Uninstall.exe").Length / 1MB, 1)
        Write-Host "  完成: dist\MAST-Legacy-Uninstall.exe ($unSize MB)"
    }
}

Write-Host ""
Write-Host "全部完成。" -ForegroundColor Green
Write-Host "产物列表："
Get-ChildItem dist\MAST*.zip, dist\MAST*.exe -ErrorAction SilentlyContinue | ForEach-Object {
    $size = [math]::Round($_.Length / 1MB, 1)
    Write-Host "  $($_.Name)  ($size MB)"
}
