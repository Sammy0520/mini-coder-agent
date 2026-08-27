[CmdletBinding()]
param()

$projectRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$authPath = Join-Path $projectRoot "auth.json"
$secureKey = Read-Host "Enter API Key (input is hidden)" -AsSecureString
$keyPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)

try {
    $plainKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($keyPointer)
    if ([string]::IsNullOrWhiteSpace($plainKey)) {
        throw "API Key cannot be empty."
    }

    $auth = [ordered]@{
        auth_mode = "apikey"
        OPENAI_API_KEY = $plainKey
    }
    $auth | ConvertTo-Json | Set-Content -LiteralPath $authPath -Encoding utf8
    Write-Host "Saved local credentials to: $authPath"
    Write-Host "The file is excluded by .gitignore and will be loaded automatically."
}
finally {
    if ($keyPointer -ne [IntPtr]::Zero) {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($keyPointer)
    }
    Remove-Variable plainKey -ErrorAction SilentlyContinue
}
