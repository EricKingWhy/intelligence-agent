$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$envPath = 'D:\intelligence-agent-backend\.env'
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Configure #304 model gate'
$form.StartPosition = 'CenterScreen'
$form.Size = [System.Drawing.Size]::new(760, 710)
$form.FormBorderStyle = [System.Windows.Forms.FormBorderStyle]::FixedDialog
$form.MaximizeBox = $false
$form.TopMost = $true
$form.Font = New-Object System.Drawing.Font('Segoe UI', 9)

function Add-Field($parent, $labelText, $top, $secret = $false) {
    $label = New-Object System.Windows.Forms.Label
    $label.Text = $labelText
    $label.Location = [System.Drawing.Point]::new(18, $top + 4)
    $label.Size = [System.Drawing.Size]::new(145, 24)
    $parent.Controls.Add($label)
    $box = New-Object System.Windows.Forms.TextBox
    $box.Location = [System.Drawing.Point]::new(170, $top)
    $box.Size = [System.Drawing.Size]::new(535, 26)
    if ($secret) { $box.UseSystemPasswordChar = $true }
    $parent.Controls.Add($box)
    return $box
}

$g1 = New-Object System.Windows.Forms.GroupBox
$g1.Text = 'Primary model'
$g1.Location = [System.Drawing.Point]::new(12, 12)
$g1.Size = [System.Drawing.Size]::new(720, 258)
$form.Controls.Add($g1)
$p1 = Add-Field $g1 'Provider' 32
$p2 = Add-Field $g1 'Model name' 82
$p3 = Add-Field $g1 'Base URL' 132
$p4 = Add-Field $g1 'API key (hidden)' 182 $true

$g2 = New-Object System.Windows.Forms.GroupBox
$g2.Text = 'Fallback model'
$g2.Location = [System.Drawing.Point]::new(12, 280)
$g2.Size = [System.Drawing.Size]::new(720, 258)
$form.Controls.Add($g2)
$f1 = Add-Field $g2 'Provider' 32
$f2 = Add-Field $g2 'Model name' 82
$f3 = Add-Field $g2 'Base URL' 132
$f4 = Add-Field $g2 'API key (hidden)' 182 $true

$note = New-Object System.Windows.Forms.Label
$note.Text = 'All fields are required. API keys are written only to the local backend .env file.'
$note.Location = [System.Drawing.Point]::new(20, 552)
$note.Size = [System.Drawing.Size]::new(710, 42)
$form.Controls.Add($note)
$save = New-Object System.Windows.Forms.Button
$save.Text = 'Save to backend .env'
$save.Location = [System.Drawing.Point]::new(430, 610)
$save.Size = [System.Drawing.Size]::new(170, 36)
$form.Controls.Add($save)
$cancel = New-Object System.Windows.Forms.Button
$cancel.Text = 'Cancel'
$cancel.Location = [System.Drawing.Point]::new(615, 610)
$cancel.Size = [System.Drawing.Size]::new(90, 36)
$form.Controls.Add($cancel)
$form.CancelButton = $cancel

$save.Add_Click({
    $fields = @(
        @{ n = 'Primary provider'; b = $p1 }, @{ n = 'Primary model'; b = $p2 },
        @{ n = 'Primary URL'; b = $p3 }, @{ n = 'Primary key'; b = $p4 },
        @{ n = 'Fallback provider'; b = $f1 }, @{ n = 'Fallback model'; b = $f2 },
        @{ n = 'Fallback URL'; b = $f3 }, @{ n = 'Fallback key'; b = $f4 }
    )
    $missing = @($fields | Where-Object { [string]::IsNullOrWhiteSpace($_.b.Text) } | ForEach-Object { $_.n })
    if ($missing.Count) {
        [System.Windows.Forms.MessageBox]::Show(('Fill in: ' + ($missing -join ', ')), 'Incomplete configuration') | Out-Null
        return
    }
    $values = @(
        $p1.Text.Trim(), $p2.Text.Trim(), $p3.Text.Trim(), $p4.Text.Trim(),
        $f1.Text.Trim(), $f2.Text.Trim(), $f3.Text.Trim(), $f4.Text.Trim()
    )
    if (@($values | Where-Object { $_ -match '[\r\n]' }).Count) {
        [System.Windows.Forms.MessageBox]::Show('Values must be single-line.', 'Invalid configuration') | Out-Null
        return
    }
    foreach ($url in @($values[2], $values[6])) {
        $uri = $null
        if (-not [uri]::TryCreate($url, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -notin @('https', 'http')) {
            [System.Windows.Forms.MessageBox]::Show('Base URL must be a valid http or https URL.', 'Invalid URL') | Out-Null
            return
        }
    }

    $names = @(
        'MODEL_PROVIDER', 'MODEL_NAME', 'MODEL_BASE_URL', 'MODEL_API_KEY',
        'FALLBACK_MODEL_PROVIDER', 'FALLBACK_MODEL_NAME', 'FALLBACK_MODEL_BASE_URL', 'FALLBACK_MODEL_API_KEY'
    )
    $old = if (Test-Path -LiteralPath $envPath) { [System.IO.File]::ReadAllText($envPath) } else { '' }
    $kept = New-Object 'System.Collections.Generic.List[string]'
    foreach ($line in [regex]::Split($old, "\r\n|\n|\r")) {
        $drop = $false
        foreach ($name in $names) {
            if ($line -match ('^\s*' + [regex]::Escape($name) + '\s*=')) { $drop = $true; break }
        }
        if (-not $drop) { $kept.Add($line) }
    }
    while ($kept.Count -and [string]::IsNullOrWhiteSpace($kept[$kept.Count - 1])) { $kept.RemoveAt($kept.Count - 1) }
    if ($kept.Count) { $kept.Add('') }
    for ($i = 0; $i -lt $names.Count; $i++) {
        if ($values[$i] -match '^[A-Za-z0-9_./:@+-]+$') { $encoded = $values[$i] }
        else { $encoded = '"' + $values[$i].Replace('\', '\\').Replace('"', '\"') + '"' }
        $kept.Add($names[$i] + '=' + $encoded)
    }

    $tmp = Join-Path ([System.IO.Path]::GetDirectoryName($envPath)) ('.env.codex-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [System.IO.File]::WriteAllText($tmp, ([string]::Join([Environment]::NewLine, $kept) + [Environment]::NewLine), (New-Object System.Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $envPath) { [System.IO.File]::Replace($tmp, $envPath, $null) }
        else { [System.IO.File]::Move($tmp, $envPath) }
        $form.Tag = 'saved'
        $form.DialogResult = [System.Windows.Forms.DialogResult]::OK
        $form.Close()
    } catch {
        [System.Windows.Forms.MessageBox]::Show('Write failed. Existing configuration was preserved.', 'Save failed') | Out-Null
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
})

$result = $form.ShowDialog()
if ($result -eq [System.Windows.Forms.DialogResult]::OK -and $form.Tag -eq 'saved') { exit 0 }
exit 2
