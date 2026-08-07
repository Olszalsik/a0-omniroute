# uninstall-omniroute.ps1
# Removes the OmniRoute Docker container (and optionally the image)
# from a Windows host. Mirrors the style of install-omniroute.ps1.
#
# What this does:
#   1. Checks for Docker CLI (no point going further without it)
#   2. Ensures the Docker daemon is running (starts Docker Desktop if not)
#   3. Looks for the OmniRoute container; exits 0 if not found (idempotent)
#   4. Stops + removes the container
#   5. Asks whether to also remove the Docker image
#   6. Verifies the container is gone
#
# What this does NOT do:
#   - Does NOT touch the Agent Zero plugin folder. That's the
#     framework's job: use Settings -> Plugins -> Uninstall.
#   - Does NOT remove Docker Desktop itself. (The user might have
#     other containers running on the host.)
#   - Does NOT touch any of the user's OmniRoute configuration
#     (env file, downloaded model cache, auth tokens). Those live
#     inside the container and are removed with it.
#
# Run via the "Remove OmniRoute gateway" button in the Agent Zero
# WebUI (Settings -> External -> OmniRoute). The browser downloads
# this file; double-click it and confirm the UAC prompt. ~10 s total.
#
# Why this is a separate script (not a hooks.uninstall() side effect):
#   The plugin folder and the Docker container have independent
#   lifecycles. Some users keep the gateway running for other tools
#   (curl, scripts, other A0 plugins). The plugin's uninstall() hook
#   removes only the plugin folder; the container is left running.
#   This script is the explicit "I really want the gateway gone too"
#   path, downloaded on demand from the WebUI.

$ErrorActionPreference = 'Stop'
$Image         = 'diegosouzapw/omniroute'
$ContainerName = 'omniroute'

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "  OK   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  WARN $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  FAIL $msg" -ForegroundColor Red }

# --------------------------------------------------------------- Step 1
Write-Step "Step 1/4: Docker CLI"
$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    $v = (& docker --version) 2>$null
    Write-OK "Docker CLI found: $v"
} else {
    Write-Host "  Docker CLI not found on PATH." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Nothing to uninstall: this host has never had Docker" -ForegroundColor Yellow
    Write-Host "  installed, so it cannot have had an OmniRoute container." -ForegroundColor Yellow
    Write-Host "  Exiting 0." -ForegroundColor Yellow
    exit 0
}

# --------------------------------------------------------------- Step 2
Write-Step "Step 2/4: Docker daemon"
# `docker info` exits 0 only when the daemon is reachable. We test with
# `docker info` rather than `docker ps` so we also catch the
# "daemon socket exists but is in a bad state" failure mode.
$daemonUp = $false
try {
    $null = docker info 2>$null
    if ($LASTEXITCODE -eq 0) { $daemonUp = $true }
} catch { }

if ($daemonUp) {
    Write-OK "Docker daemon is reachable"
} else {
    Write-Host "  Docker daemon is not reachable. Attempting to start Docker Desktop..." -ForegroundColor Yellow
    $dockerDesktop = "C:\Program Files\Docker\Docker\Docker Desktop.exe"
    if (Test-Path $dockerDesktop) {
        try {
            Start-Process -FilePath $dockerDesktop -ErrorAction SilentlyContinue
        } catch { }
    }
    Write-Host "  Waiting up to 60 seconds for the daemon..."
    for ($i = 1; $i -le 60; $i++) {
        Start-Sleep -Seconds 1
        try {
            $null = docker info 2>$null
            if ($LASTEXITCODE -eq 0) {
                $daemonUp = $true
                Write-OK "Docker daemon is up (after $i seconds)"
                break
            }
        } catch { }
    }
    if (-not $daemonUp) {
        Write-Err "Docker daemon did not become reachable within 60 seconds."
        Write-Host ""
        Write-Host "  Open Docker Desktop from the Start menu, wait for the whale"
        Write-Host "  icon to settle (no animation), and re-run this script." -ForegroundColor Yellow
        exit 1
    }
}

# --------------------------------------------------------------- Step 3
Write-Step "Step 3/4: Find container '$ContainerName'"
# `docker ps -a` (not `docker ps`) catches both running and stopped
# containers. We use the exact-name filter so a hypothetical
# `myomniroute` container is not accidentally caught.
$existing = $null
try {
    $existing = docker ps -a --filter "name=^${ContainerName}$" --format '{{.Names}}' 2>$null
} catch {
    Write-Err "Failed to query Docker for the container list: $_"
    exit 1
}

if ($existing -ne $ContainerName) {
    Write-OK "No container named '$ContainerName' found on this host."
    Write-Host ""
    Write-Host "  Nothing to do. If you expected a container to be here, double-check" -ForegroundColor White
    Write-Host "  the name with:  docker ps -a --filter name=omniroute" -ForegroundColor White
    Write-Host "  (the filter is exact-match, anchored with ^...$)." -ForegroundColor White
    Write-Host ""
    Write-Host "  Exiting 0." -ForegroundColor Green
    exit 0
}
Write-OK "Container '$ContainerName' found."

# --------------------------------------------------------------- Step 4
Write-Step "Step 4/4: Remove container '$ContainerName' (and maybe the image)"

# Stop the container first. `docker stop` returns the container name on
# success; if the container is already stopped, the command exits
# non-zero on some Docker versions, but the state we want is still
# reached. We treat a non-zero exit as a warning rather than a
# failure, because the next step (`docker rm`) handles both states.
try {
    $stopOutput = docker stop $ContainerName 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-OK "Container stopped"
    } else {
        # Most common reason: container was already stopped.
        Write-Warn "docker stop exited $LASTEXITCODE (continuing; the container may have already been stopped)"
    }
} catch {
    Write-Warn "docker stop raised: $_ (continuing)"
}

# Remove the container. `docker rm` on an already-removed container
# fails, but we re-check at the end and report.
try {
    $rmOutput = docker rm $ContainerName 2>&1
    if ($LASTEXITCODE -ne 0) {
        throw "docker rm exited ${LASTEXITCODE}: $rmOutput"
    }
    Write-OK "Container removed"
} catch {
    Write-Err "Failed to remove container '$ContainerName'"
    Write-Host "  $_" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Common causes: another process has files open inside the" -ForegroundColor Yellow
    Write-Host "  container, or the container name has a typo. Try manually:" -ForegroundColor Yellow
    Write-Host "    docker ps -a --filter name=$ContainerName" -ForegroundColor Yellow
    Write-Host "    docker rm -f $ContainerName" -ForegroundColor Yellow
    exit 1
}

# Verify the container is actually gone. We re-query because `docker rm`
# can succeed but leave a stopped container with the same name in edge
# cases (e.g. a `--restart` policy fight with the daemon).
$verify = docker ps -a --filter "name=^${ContainerName}$" --format '{{.Names}}' 2>$null
if ($verify -eq $ContainerName) {
    Write-Err "Verification failed: container '$ContainerName' is still visible to Docker."
    Write-Host "  Try:  docker ps -a --filter name=$ContainerName" -ForegroundColor Yellow
    exit 1
}
Write-OK "Container '$ContainerName' is gone."

# Ask whether to also remove the Docker image. The image is the
# ~500 MB pull from the registry; keeping it around means the user
# can reinstall without a network round trip. Default to "no" -- the
# user might want to reinstall.
Write-Host ""
$imageAns = Read-Host "  Also remove the $Image Docker image? (saves ~500 MB; next install will re-pull) [y/N]"
if ($imageAns -eq 'y') {
    try {
        $rmiOutput = docker rmi $Image 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "docker rmi exited ${LASTEXITCODE}: $rmiOutput"
        }
        Write-OK "Image '$Image' removed"
    } catch {
        Write-Warn "Failed to remove image '$Image' (the container is gone, but the image remains):"
        Write-Host "  $_" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  This is non-fatal: the gateway is already gone. To remove the" -ForegroundColor Yellow
        Write-Host "  image by hand, run:" -ForegroundColor Yellow
        Write-Host "    docker rmi $Image" -ForegroundColor Yellow
    }
} else {
    Write-OK "Image '$Image' kept (next install will skip the re-pull)"
}

Write-Host ""
Write-Host "===============================================" -ForegroundColor Green
Write-Host "  OmniRoute gateway is REMOVED" -ForegroundColor Green
Write-Host "===============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  The Docker container '$ContainerName' is gone." -ForegroundColor White
Write-Host "  The Agent Zero plugin (Settings -> Plugins -> OmniRoute)" -ForegroundColor White
Write-Host "  is unaffected: it will now show 'Not installed' until you" -ForegroundColor White
Write-Host "  run the installer again, or you can uninstall the plugin" -ForegroundColor White
Write-Host "  from Settings -> Plugins." -ForegroundColor White
Write-Host ""
Write-Host "To reinstall: open the Agent Zero WebUI, go to" -ForegroundColor White
Write-Host "Settings -> External -> OmniRoute, and click" -ForegroundColor White
Write-Host "'Download & run installer'." -ForegroundColor White
