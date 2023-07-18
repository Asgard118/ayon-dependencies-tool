# Receive first positional argument
$FunctionName=$ARGS[0]
$arguments=@()
if ($ARGS.Length -gt 1) {
    $arguments = $ARGS[1..($ARGS.Length - 1)]
}

$poetry_verbosity="-vv"

$current_dir = Get-Location
$script_dir_rel = Split-Path -Path $MyInvocation.MyCommand.Definition -Parent
$script_dir = (Get-Item $script_dir_rel).FullName


function Default-Func {
    Write-Host ""
    Write-Host "Ayon dependency package tool"
    Write-Host ""
    Write-Host "Usage: ./manage.ps1 [target]"
    Write-Host ""
    Write-Host "Runtime targets:"
    Write-Host "  install                       Install Poetry and update venv by lock file."
    Write-Host "  set_env                       Set all env vars in .env file."
    Write-Host "  listen                        Start listener on a server."
    Write-Host "  create                        Create dependency package for single bundle."
    Write-Host ""
}

function Exit-WithCode($exitcode) {
   # Only exit this host process if it's a child of another PowerShell parent process...
   $parentPID = (Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$PID" | Select-Object -Property ParentProcessId).ParentProcessId
   $parentProcName = (Get-CimInstance -ClassName Win32_Process -Filter "ProcessId=$parentPID" | Select-Object -Property Name).Name
   if ('powershell.exe' -eq $parentProcName) { $host.SetShouldExit($exitcode) }
   Restore-Cwd
   exit $exitcode
}

function Install-Poetry() {
    Write-Host ">>> Installing Poetry ... "
    $python = "python"
    if (Get-Command "pyenv" -ErrorAction SilentlyContinue) {
        if (-not (Test-Path -PathType Leaf -Path "$($repo_root)\.python-version")) {
            $result = & pyenv global
            if ($result -eq "no global version configured") {
                Write-Host "!!! ", "Using pyenv but having no local or global version of Python set."
                Exit-WithCode 1
            }
        }
        $python = & pyenv which python
    }

    $env:POETRY_HOME="$script_dir\.poetry"
    $env:POETRY_VERSION="1.3.2"
    (Invoke-WebRequest -Uri https://install.python-poetry.org/ -UseBasicParsing).Content | & $($python) -
}

function Change-Cwd() {
    Set-Location -Path $script_dir
}

function Restore-Cwd() {
    Set-Location -Path $current_dir
}

function install {
    # install dependencies for tool
     if (-not (Test-Path -PathType Container -Path "$($env:POETRY_HOME)\bin")) {
        Install-Poetry
    }

    Change-Cwd
    Write-Host ">>> ", "Creating venv ... ", "$($script_dir)"
    & "$env:POETRY_HOME\bin\poetry" config virtualenvs.in-project true --local
    & "$env:POETRY_HOME\bin\poetry" config virtualenvs.create true --local

    Write-Host ">>> ", "Poetry config ... "
    & "$env:POETRY_HOME\bin\poetry" config --list
    & "$env:POETRY_HOME\bin\poetry" install --no-interaction --no-root --ansi  $poetry_verbosity
}

function set_env {
    # set all env vars in .env file
    if (-not (Test-Path "$($script_dir)\.env")) {
        Write-Host "!!! ", ".env file must be prepared!" -ForegroundColor red
        Exit-WithCode 1
    } else {
        $content = Get-Content -Path "$($script_dir)\.env" -Encoding UTF8 -Raw
        foreach($line in $content.split()) {
            if ($line){
                $parts = $line.split("=")
                $varName = $parts[0]
                $value = $parts[1]
                Write-Host "Setting $varName with $value"
                Set-Item "env:$varName" $value
            }
        }
    }
}

function main {
    if ($FunctionName -eq $null)
    {
        Default-Func
    } elseif ($FunctionName -eq "install") {
        Change-Cwd
        install
    } elseif ($FunctionName -eq "listen") {
        Change-Cwd
        set_env
        & "$env:POETRY_HOME\bin\poetry" run python "$($script_dir)\service\listener.py" @arguments
    } elseif ($FunctionName -eq "set_env") {
        Change-Cwd
        set_env
    } elseif ($FunctionName -eq "create") {
        Change-Cwd
        set_env
        & "$env:POETRY_HOME\bin\poetry" run python "$($script_dir)\dependencies" create @arguments
    } else {
        Write-Host "Unknown function \"$FunctionName\""
        Default-Func
    }
    Restore-Cwd
}

if (-not (Test-Path 'env:POETRY_HOME')) {
    $env:POETRY_HOME = "$script_dir\.poetry"
}

main