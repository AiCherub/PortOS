$ErrorActionPreference = 'Stop'

# Windows ICU returns the IANA ID used by the Linux images.
Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
using System.Text;

public static class PortOSHostTimeZone
{
    [DllImport("icu.dll", CharSet = CharSet.Unicode, ExactSpelling = true)]
    public static extern int ucal_getDefaultTimeZone(StringBuilder id, int capacity, ref int status);
}
'@

$zone = New-Object System.Text.StringBuilder 128
$status = 0
$length = [PortOSHostTimeZone]::ucal_getDefaultTimeZone($zone, $zone.Capacity, [ref]$status)
if ($status -gt 0 -or $length -eq 0 -or $zone.ToString() -eq 'Etc/Unknown') {
    throw 'Could not determine the Windows host time zone.'
}

$zone.ToString()
