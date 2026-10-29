<#
.SYNOPSIS
  Verify an owner login against the production edge without echoing the password.
  Prompts (hidden) for the password; prints only HTTP statuses and non-secret fields.
#>
param(
    [string] $Server = "https://work.zazavoice.com",
    [string] $Email = "zazaai4u@gmail.com",
    [string] $Slug = "zaza-internal"
)
$ErrorActionPreference = "Stop"
$secure = Read-Host -AsSecureString "Owner password for $Email"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
try {
    $plain = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
    $jar = New-TemporaryFile
    $token = (curl.exe -s -c $jar "$Server/app/login" | ConvertFrom-Json).login_token
    $status = curl.exe -s -b $jar -c $jar -o NUL -w "%{http_code}" -X POST -H "Origin: $Server" `
        --data-urlencode "email=$Email" --data-urlencode "password=$plain" --data-urlencode "login_token=$token" "$Server/app/login"
    "login                      -> HTTP $status"
    if ($status -ne "200") { "Login failed. Check the password (HTTP 429 means too many attempts)."; return }
    foreach ($p in "/app/session", "/app/orgs/$Slug", "/app/orgs/$Slug/employees") {
        $body = curl.exe -s -b $jar -w "`nHTTP %{http_code}" "$Server$p"
        "GET $p`n$body`n"
    }
    $status = curl.exe -s -b $jar -c $jar -o NUL -w "%{http_code}" -X POST -H "Origin: $Server" "$Server/app/logout"
    "logout                     -> HTTP $status"
} finally {
    $plain = $null
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
    Remove-Item $jar -ErrorAction SilentlyContinue
}
