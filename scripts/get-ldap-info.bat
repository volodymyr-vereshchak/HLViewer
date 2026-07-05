@echo off
setlocal EnableDelayedExpansion
chcp 65001 >nul

rem ============================================================
rem  get-ldap-info.bat
rem  Run on ANY domain-joined Windows machine to collect the
rem  values needed for HLViewer .env.v2:
rem      LDAP_SERVER  = ldap://<domain controller>
rem      LDAP_DOMAIN  = <UPN suffix>
rem  Results are shown on screen and saved to ldap-info.txt
rem  next to this script. No changes are made to the system.
rem ============================================================

set "OUT=%~dp0ldap-info.txt"
echo LDAP discovery report - %DATE% %TIME% > "%OUT%"
echo Machine: %COMPUTERNAME%  User: %USERNAME% >> "%OUT%"
echo. >> "%OUT%"

echo ===== 1. Domain environment variables ===== >> "%OUT%"
echo USERDNSDOMAIN = %USERDNSDOMAIN% >> "%OUT%"
echo USERDOMAIN    = %USERDOMAIN% >> "%OUT%"
echo LOGONSERVER   = %LOGONSERVER% >> "%OUT%"
echo. >> "%OUT%"

echo ===== 2. User principal name (exact bind format) ===== >> "%OUT%"
"%SystemRoot%\System32\whoami.exe" /upn >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ===== 3. Domain controller details (nltest) ===== >> "%OUT%"
nltest /dsgetdc:%USERDNSDOMAIN% >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ===== 4. All LDAP servers of the domain (DNS SRV) ===== >> "%OUT%"
nslookup -type=SRV _ldap._tcp.%USERDNSDOMAIN% >> "%OUT%" 2>&1
echo. >> "%OUT%"

rem --- Build the suggested DC FQDN: <logonserver>.<dns domain> ---
set "DC=%LOGONSERVER:~2%"
set "DCFQDN=%DC%.%USERDNSDOMAIN%"

echo ===== 5. Port checks on %DCFQDN% ===== >> "%OUT%"
echo LDAP 389: >> "%OUT%"
powershell -NoProfile -Command "(Test-NetConnection -ComputerName '%DCFQDN%' -Port 389 -WarningAction SilentlyContinue).TcpTestSucceeded" >> "%OUT%" 2>&1
echo LDAPS 636: >> "%OUT%"
powershell -NoProfile -Command "(Test-NetConnection -ComputerName '%DCFQDN%' -Port 636 -WarningAction SilentlyContinue).TcpTestSucceeded" >> "%OUT%" 2>&1
echo. >> "%OUT%"

echo ===== 6. SUGGESTED .env.v2 VALUES ===== >> "%OUT%"
echo LDAP_ENABLED=true >> "%OUT%"
echo LDAP_SERVER=ldap://%DCFQDN% >> "%OUT%"
echo LDAP_DOMAIN=%USERDNSDOMAIN% >> "%OUT%"
echo LDAP_USE_SSL=false >> "%OUT%"
echo. >> "%OUT%"
echo Notes: >> "%OUT%"
echo  - If port 636 above is True, prefer LDAPS: >> "%OUT%"
echo      LDAP_SERVER=ldaps://%DCFQDN%:636  and  LDAP_USE_SSL=true >> "%OUT%"
echo  - Any DC from section 4 works as LDAP_SERVER, not only the logon one. >> "%OUT%"
echo  - The HLViewer server must be able to resolve and reach this host/port. >> "%OUT%"

type "%OUT%"
echo.
echo Report saved to: %OUT%
pause
