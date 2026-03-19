$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$codexBin = Join-Path $scriptDir "codex-hsog.exe"

if (-not (Test-Path $codexBin)) {
    Write-Error "codex-hsog.exe not found next to launcher: $codexBin"
}

if (-not $env:HSOG_API_KEY) {
    $secureKey = Read-Host "HSOG API key" -AsSecureString
    $bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)
    try {
        $env:HSOG_API_KEY = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    } finally {
        if ($bstr -ne [IntPtr]::Zero) {
            [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
        }
    }
}

if (-not $env:HSOG_API_KEY) {
    Write-Error "HSOG_API_KEY is required."
}

& $codexBin `
    -c 'model_provider="hs_og"' `
    -c 'model="gpt-5.2"' `
    -c 'model_providers.hs_og={name="HS_OG",base_url="https://llm-proxy.imla.hs-offenburg.de/v1",env_key="HSOG_API_KEY",wire_api="responses",fallback_chat=true,fallback_chat_path="/chat/completions"}' `
    @args

exit $LASTEXITCODE
