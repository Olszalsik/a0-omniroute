# install-omniroute.ps1
# One-shot install script for OmniRoute on Windows (Docker path).
#
# What this does:
#   1. Checks for Docker CLI (install Docker Desktop if missing)
#   2. Ensures the Docker daemon is running (starts Docker Desktop if not)
#   3. Pulls the diegosouzapw/omniroute image
#   4. Starts the gateway as a Docker container with --restart=unless-stopped
#      so it survives Windows host reboots automatically
#   5. Verifies the gateway is responding on http://localhost:8080/v1/models
#   6. Prints clear next steps for the Agent Zero WebUI
#
# Run once on your Windows host. The Agent Zero Docker container reaches
# the gateway via host.docker.internal:8080 (the plugin's default
# base_url, which corresponds to this container's -p 8080:20128 mapping;
# 20128 is the upstream OmniRoute image's baked-in ENV PORT, NOT 2012).
#
# Re-run safety: every step is idempotent. Running the script a second
# time will detect the existing container, ensure the restart policy is
# still correct, and (re)start it. Useful for the WebUI's "Recover
# OmniRoute" button, which downloads and re-runs this script.
#
# Usage (PowerShell, no admin required for OmniRoute itself; admin is
# required for `docker run` because the Docker CLI talks to a daemon
# running as a Windows service). The script is fetched from the
# local Agent Zero asset server at runtime -- the previous
# `irm https://raw.githubusercontent.com/... | iex` one-liner was
# removed because the public repo it pointed at did not exist
# (raw.githubusercontent.com 404s), and we want the install path
# to have no external URL dependency. Use the "Download & run
# installer" button in the Agent Zero WebUI (Settings -> External
# -> OmniRoute) to get a copy of this file.
#
# Pre-requisite (the silent break):
#   "Start Docker Desktop at logon" must be enabled in
#   Docker Desktop -> Settings -> General. This is the default on a fresh
#   Docker Desktop install; if the user has turned it off, the container
#   will NOT come back automatically after a host reboot. We surface this
#   in the success banner so it stays in the user's mind.

$ErrorActionPreference = 'Stop'
$Port          = 8080
# IMPORTANT: must match the OmniRoute image's ENV PORT, which is 20128
# (see diegosouzapw/OmniRoute Dockerfile: `ENV PORT=20128`, `EXPOSE 20128`).
# If you set this to anything else, the container starts but no process
# binds inside it, host port $Port stays unbound, and the probe times out
# with a misleading "Gateway did not respond" error.
$ContainerPort = 20128
$Image         = 'diegosouzapw/omniroute'
$ContainerName = 'omniroute'
$RestartPolicy = 'unless-stopped'

function Write-Step($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Write-OK($msg)   { Write-Host "  OK   $msg" -ForegroundColor Green }
function Write-Warn($msg) { Write-Host "  WARN $msg" -ForegroundColor Yellow }
function Write-Err($msg)  { Write-Host "  FAIL $msg" -ForegroundColor Red }

# --------------------------------------------------------------- Step 1
Write-Step "Step 1/5: Docker CLI"
$docker = Get-Command docker -ErrorAction SilentlyContinue
if ($docker) {
    $v = (& docker --version) 2>$null
    Write-OK "Docker CLI found: $v"
} else {
    Write-Host "  Docker CLI not found on PATH." -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Install Docker Desktop for Windows:" -ForegroundColor Yellow
    Write-Host "    https://www.docker.com/products/docker-desktop/" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  During the installer, leave the default options enabled,"
    Write-Host "  especially 'Start Docker Desktop at logon' under"
    Write-Host "  Settings -> General. This is what makes OmniRoute come back"
    Write-Host "  automatically after a Windows reboot -- without it, you will"
    Write-Host "  have to start Docker Desktop by hand every morning."
    exit 1
}

# --------------------------------------------------------------- Step 2
Write-Step "Step 2/5: Docker daemon"
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
    # Standard install path. If the user has Docker Desktop in a custom
    # location, the Start-Process will fail silently and we'll report it
    # below. We do NOT try to find it in alternate locations -- 99% of
    # users have it here, and the failure message tells them to open it
    # from the Start menu if we miss.
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
        Write-Host "  Common causes:" -ForegroundColor Yellow
        Write-Host "    - Docker Desktop is still starting (look for the whale icon in the system tray)" -ForegroundColor Yellow
        Write-Host "    - WSL 2 is not installed or not updated (Docker Desktop requires it on Windows 10/11)" -ForegroundColor Yellow
        Write-Host "    - Hyper-V is disabled on Windows 10 Pro" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Open Docker Desktop from the Start menu, wait for the whale"
        Write-Host "  icon to settle (no animation), and re-run this script." -ForegroundColor Yellow
        exit 1
    }
}

# --------------------------------------------------------------- Step 3
Write-Step "Step 3/5: Pull $Image"
# `docker pull` is idempotent -- it skips the network round-trip when
# the local copy matches the registry. We do NOT pin a tag (e.g.
# `:3.8.40`) so the user can pick up bugfixes from upstream by running
# this script. If they want a pinned version, they can edit this file.
try {
    docker pull $Image 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        throw "docker pull exited $LASTEXITCODE"
    }
    Write-OK "Image pulled (or already up to date)"
} catch {
    Write-Err "Failed to pull $Image"
    Write-Host "  $_" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "  Common causes: no internet, corporate proxy blocking the"
    Write-Host "  Docker Hub registry, or an outdated Docker Desktop that"
    Write-Host "  does not support the image's platform. Try:" -ForegroundColor Yellow
    Write-Host "    docker pull $Image" -ForegroundColor Yellow
    Write-Host "  in a manual PowerShell to see the full error." -ForegroundColor Yellow
    exit 1
}

# --------------------------------------------------------------- Step 4
Write-Step "Step 4/5: Run container '$ContainerName' (--restart=$RestartPolicy)"

# If a container with this name already exists, do NOT try to recreate
# it -- that would lose the user's state (downloaded models cache,
# auth tokens, env-file config). Instead, ensure the restart policy is
# correct and start it. This makes the script safe to re-run, which
# the WebUI's "Recover OmniRoute" button relies on.
$existing = $null
try {
    $existing = docker ps -a --filter "name=^${ContainerName}$" --format '{{.Names}}' 2>$null
} catch { }

if ($existing -eq $ContainerName) {
    # === NEW (v2.6.2): verify port mapping before reusing the existing container. ===
    # Why this matters: previous installers (and earlier drafts of this one) published
    # host $Port -> container 2012 instead of 20128. Such a container would start, but
    # no process would bind inside it, so host port $Port stayed unbound and the probe
    # timed out with a misleading "Gateway did not respond" error. Re-running this
    # script on such a stale container would silently do nothing (because the
    # existing-container branch only ensures restart policy + start). Detect the
    # mismatch here and force a recreate so subsequent runs self-heal.
    try {
        $portMapLines = docker port $ContainerName 2>$null
        if ($LASTEXITCODE -ne 0) { $portMapLines = @() }
    } catch { $portMapLines = @() }
    $portMap = ($portMapLines -join "`n")
    # Match e.g. "20128/tcp -> 0.0.0.0:8080" or "20128/tcp -> [::]:8080".
    $expectedMapping = [regex]::Escape("${ContainerPort}/tcp") + '\s+->\s+(0\.0\.0\.0|\*|\[::\]):' + [regex]::Escape("$Port") + '\b'
    if ($portMap -match $expectedMapping) {
        Write-OK "Container '$ContainerName' already exists with port $Port -> $ContainerPort -- ensuring restart policy and starting"
    } else {
        Write-Warn "Container '$ContainerName' exists but its port mapping is wrong or missing."
        Write-Host "    current port publish:" -ForegroundColor Yellow
        if ($portMap -and $portMap.Trim()) {
            foreach ($line in ($portMap -split "`n")) { Write-Host "      | $line" -ForegroundColor Yellow }
        } else {
            Write-Host "      (no published ports -- the gateway never bound inside the container)" -ForegroundColor Yellow
        }
        Write-Host "    expected:             ${Port} (host) -> ${ContainerPort} (container)" -ForegroundColor Yellow
        Write-Warn "Removing and re-creating with the correct mapping now." -ForegroundColor Yellow
        Write-Host "    (This drops any state currently in the container -- downloaded model" -ForegroundColor Yellow
        Write-Host "     cache, auth tokens, env-file config. The image is already cached locally" -ForegroundColor Yellow
        Write-Host "     so the recreate takes ~5 s; provider auth tokens can be re-pasted in the" -ForegroundColor Yellow
        Write-Host "     gateway Settings page.)" -ForegroundColor Yellow
        try {
            docker rm -f $ContainerName 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) {
                throw "docker rm -f exited $LASTEXITCODE"
            }
            Write-OK "Removed stale container '$ContainerName'"
            $existing = $null  # fall through to the fresh-container branch below
        } catch {
            Write-Err "Failed to remove stale container '$ContainerName': $_"
            exit 1
        }
    }
}
if ($existing -eq $ContainerName) {
    # Port mapping was correct -- reuse the existing container.
    try {
        docker update --restart=$RestartPolicy $ContainerName 2>&1 | Out-Null
        if ($LASTEXITCODE -ne 0) {
            Write-Warn "docker update exited $LASTEXITCODE (continuing -- the container may still start)"
        } else {
            Write-OK "Restart policy: $RestartPolicy"
        }
    } catch {
        Write-Warn "docker update raised: $_ (continuing)"
    }
    # Only start if not already running. `docker start` on a running
    # container is a no-op so this is safe.
    docker start $ContainerName 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
        Write-Err "Failed to start existing container '$ContainerName' (exit $LASTEXITCODE)"
        Write-Host "  Try: docker logs $ContainerName --tail 50" -ForegroundColor Yellow
        exit 1
    }
    Write-OK "Container started"
}
if ($existing -ne $ContainerName) {
    # Fresh container. Free the host port first so the port-publish
    # doesn't fail with "bind: address already in use". We check the
    # port, find the owning process, and offer to kill it.
    $portInUse = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($portInUse) {
        $pid_ = $portInUse.OwningProcess
        $proc = Get-Process -Id $pid_ -ErrorAction SilentlyContinue
        Write-Warn "Port $Port is in use by $($proc.ProcessName) (PID $pid_)"
        $ans = Read-Host "  Kill it and continue? [y/N]"
        if ($ans -eq 'y') {
            Stop-Process -Id $pid_ -Force -ErrorAction SilentlyContinue
            Start-Sleep -Seconds 2
            Write-OK "Killed PID $pid_"
        } else {
            Write-Err "Refusing to start. Free port $Port and re-run, or edit `$Port at the top of this script."
            exit 1
        }
    } else {
        Write-OK "Port $Port is free"
    }

    try {
        # -d = detached (background). --name makes the container
        # addressable by name. --restart=unless-stopped is the
        # "survive a host reboot" policy. -p 8080:20128 publishes
        # the container's port 20128 (the upstream image's ENV PORT,
        # NOT 2012) as port 8080 on the host. Port 8080 is the
        # plugin's documented default -- 20128 is the upstream
        # gateway's actual listen port.
        # We build the port-publish string in a separate variable to
        # sidestep PowerShell's "${var:ns}" namespace syntax inside
        # an interpolated double-quoted string.
        $portPublish = "${Port}:${ContainerPort}"
        $runOutput = docker run -d `
            --name $ContainerName `
            --restart $RestartPolicy `
            -p $portPublish `
            $Image 2>&1
        if ($LASTEXITCODE -ne 0) {
            throw "docker run exited ${LASTEXITCODE}: $runOutput"
        }
        Write-OK "Container '$ContainerName' started with restart policy '$RestartPolicy'"
    } catch {
        Write-Err "Failed to start container '$ContainerName'"
        Write-Host "  $_" -ForegroundColor Yellow
        Write-Host ""
        Write-Host "  Hint: if the error is 'port is already allocated', another" -ForegroundColor Yellow
        Write-Host "  container is using host port $Port. Find it with:" -ForegroundColor Yellow
        Write-Host "    docker ps --filter publish=$Port" -ForegroundColor Yellow
        exit 1
    }
}

# --------------------------------------------------------------- Step 5
Write-Step "Step 5/5: Verifying gateway on http://localhost:$Port"

# Probe multiple addresses. On Windows, `localhost` resolves to `[::1]`
# (IPv6) first; if the container's port-publish is bound to IPv4 only
# (the Docker Desktop default for Windows), the IPv6 connection will
# time out. We try 127.0.0.1 first because that's the most common
# binding. This is the same loop the previous installer used, just
# re-targeted at the Docker-published port.
$probeAddresses = @('127.0.0.1', 'localhost', '[::1]')
$ready = $false
$lastError = $null
$lastUrl = $null
for ($i = 1; $i -le 45; $i++) {
    Start-Sleep -Seconds 1
    foreach ($addr in $probeAddresses) {
        $url = "http://${addr}:$Port/v1/models"
        $lastUrl = $url
        try {
            $null = Invoke-RestMethod -Uri $url -TimeoutSec 2 -ErrorAction Stop
            $ready = $true
            break
        } catch {
            $lastError = $_.Exception.Message
        }
    }
    if ($ready) { break }
}

if (-not $ready) {
    Write-Err "Gateway did not respond within 45 seconds."
    Write-Host ""
    Write-Host "  Diagnostic dump:" -ForegroundColor Yellow
    # 1. Is the container running?
    $inspect = $null
    try { $inspect = docker inspect --format '{{.State.Status}} (started {{.State.StartedAt}})' $ContainerName 2>$null } catch { }
    if ($inspect) {
        Write-Host "    - container state: $inspect" -ForegroundColor Yellow
    } else {
        Write-Host "    - container '$ContainerName' is NOT visible to docker inspect" -ForegroundColor Red
    }
    # 2. Is the host port bound?
    $portListener = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue
    if ($portListener) {
        Write-Host "    - host port $Port is bound (PID $($portListener.OwningProcess))" -ForegroundColor Yellow
    } else {
        Write-Host "    - host port $Port is NOT bound" -ForegroundColor Red
    }
    # 3. Last HTTP error from the probe loop
    if ($lastError) {
        Write-Host "    - last HTTP error: $lastError" -ForegroundColor Yellow
    }
    if ($lastUrl) {
        Write-Host "    - last URL probed: $lastUrl" -ForegroundColor Yellow
    }
    # 4. Container logs -- the real reason the gateway isn't answering.
    # "port already in use" inside the container, "env file missing",
    # "auth token not configured" -- all of these surface here.
    Write-Host ""
    Write-Host "  Container logs (last 50 lines of 'docker logs'):" -ForegroundColor Yellow
    try {
        $logs = docker logs $ContainerName --tail 50 2>&1
        if ($logs) {
            foreach ($line in ($logs -split "`n")) {
                Write-Host "    | $line" -ForegroundColor Yellow
            }
        } else {
            Write-Host "    (no log output -- the container may not have started yet)" -ForegroundColor Yellow
        }
    } catch {
        Write-Host "    (could not read container logs: $_)" -ForegroundColor Yellow
    }
    Write-Host ""
    Write-Host "  Try running these manually in a PowerShell to see the real error:" -ForegroundColor Yellow
    Write-Host "    docker ps -a --filter name=$ContainerName" -ForegroundColor Yellow
    Write-Host "    docker logs $ContainerName --tail 50" -ForegroundColor Yellow
    exit 1
}

# Get model count for the success banner
try {
    $resp = Invoke-RestMethod -Uri "http://localhost:$Port/v1/models" -TimeoutSec 5
    $count = ($resp.data | Measure-Object).Count
} catch { $count = '?' }

Write-Host ""
Write-Host "===============================================" -ForegroundColor Green
Write-Host "  OmniRoute is ONLINE on port $Port" -ForegroundColor Green
Write-Host "  Models exposed: $count" -ForegroundColor Green
Write-Host "===============================================" -ForegroundColor Green
Write-Host ""
Write-Host "  Dashboard:    http://localhost:$Port/" -ForegroundColor White
Write-Host "  API base:     http://localhost:$Port/v1" -ForegroundColor White
Write-Host "  From A0:      http://host.docker.internal:$Port/v1" -ForegroundColor White
Write-Host ""
Write-Host "  Docker restart policy: $RestartPolicy" -ForegroundColor White
Write-Host "    The container will come back automatically when Docker" -ForegroundColor White
Write-Host "    Desktop starts (at logon, if 'Start Docker Desktop at" -ForegroundColor White
Write-Host "    logon' is enabled in Docker Desktop -> Settings -> General --" -ForegroundColor White
Write-Host "    the default on a fresh Docker Desktop install)." -ForegroundColor White
Write-Host ""
Write-Host "Next steps:" -ForegroundColor White
Write-Host "  1. Go back to the Agent Zero WebUI" -ForegroundColor White
Write-Host "  2. Refresh the page (F5)" -ForegroundColor White
Write-Host "  3. The OmniRoute status pill (top-right) should turn GREEN" -ForegroundColor White
Write-Host "  4. All $count models appear in the model picker as omniroute/<model-id>" -ForegroundColor White
Write-Host ""
Write-Host "The container will keep running in the background. To stop it:" -ForegroundColor White
Write-Host "  docker stop $ContainerName" -ForegroundColor White
Write-Host "To start it again (the WebUI's 'Recover OmniRoute' button does the same):" -ForegroundColor White
Write-Host "  docker start $ContainerName" -ForegroundColor White
Write-Host "To remove it entirely:" -ForegroundColor White
Write-Host "  docker rm -f $ContainerName" -ForegroundColor White
