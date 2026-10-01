$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
$envPath = 'D:\intelligence-agent-backend\.env'
$form = New-Object Windows.Forms.Form
$form.Text = '配置 #304 真实门禁模型'
$form.StartPosition = 'CenterScreen'
$form.Size = [Drawing.Size]::new(760, 710)
$form.FormBorderStyle = [Windows.Forms.FormBorderStyle]::FixedDialog
$form.MaximizeBox = $false
$form.TopMost = $true
$form.Font = New-Object Drawing.Font('Microsoft YaHei UI', 9)

function Add-Field($parent, $labelText, $top, $secret = $false) {
    $label = New-Object Windows.Forms.Label
    $label.Text = $labelText
    $label.Location = [Drawing.Point]::new(18, $top + 4)
    $label.Size = [Drawing.Size]::new(145, 24)
    $parent.Controls.Add($label)
    $box = New-Object Windows.Forms.TextBox
    $box.Location = [Drawing.Point]::new(170, $top)
    $box.Size = [Drawing.Size]::new(535, 26)
    if ($secret) { $box.UseSystemPasswordChar = $true }
    $parent.Controls.Add($box)
    return $box
}

$g1 = New-Object Windows.Forms.GroupBox
$g1.Text = '主模型'
$g1.Location = [Drawing.Point]::new(12, 12)
$g1.Size = [Drawing.Size]::new(720, 258)
$form.Controls.Add($g1)
$p1 = Add-Field $g1 '厂商 / Provider' 32
$p2 = Add-Field $g1 '模型名' 82
$p3 = Add-Field $g1 'Base URL' 132
$p4 = Add-Field $g1 'API key（隐藏输入）' 182 $true

$g2 = New-Object Windows.Forms.GroupBox
$g2.Text = '备用模型'
$g2.Location = [Drawing.Point]::new(12, 280)
$g2.Size = [Drawing.Size]::new(720, 258)
$form.Controls.Add($g2)
$f1 = Add-Field $g2 '厂商 / Provider' 32
$f2 = Add-Field $g2 '模型名' 82
$f3 = Add-Field $g2 'Base URL' 132
$f4 = Add-Field $g2 'API key（隐藏输入）' 182 $true

$note = New-Object Windows.Forms.Label
$note.Text = '请填写完整配置。密钥仅写入本机 backend .env，不会显示在终端。'
$note.Location = [Drawing.Point]::new(20, 552)
$note.Size = [Drawing.Size]::new(710, 42)
$form.Controls.Add($note)
$save = New-Object Windows.Forms.Button
$save.Text = '保存到 backend .env'
$save.Location = [Drawing.Point]::new(430, 610)
$save.Size = [Drawing.Size]::new(170, 36)
$form.Controls.Add($save)
$cancel = New-Object Windows.Forms.Button
$cancel.Text = '取消'
$cancel.Location = [Drawing.Point]::new(615, 610)
$cancel.Size = [Drawing.Size]::new(90, 36)
$form.Controls.Add($cancel)
$form.CancelButton = $cancel

$save.Add_Click({
    $fields = @(
        @{ n = '主模型 Provider'; b = $p1 }, @{ n = '主模型名'; b = $p2 },
        @{ n = '主模型 Base URL'; b = $p3 }, @{ n = '主模型 API key'; b = $p4 },
        @{ n = '备用模型 Provider'; b = $f1 }, @{ n = '备用模型名'; b = $f2 },
        @{ n = '备用模型 Base URL'; b = $f3 }, @{ n = '备用模型 API key'; b = $f4 }
    )
    $missing = @($fields | Where-Object { [string]::IsNullOrWhiteSpace($_.b.Text) } | ForEach-Object { $_.n })
    if ($missing.Count) {
        [Windows.Forms.MessageBox]::Show(('请填写：' + ($missing -join '、')), '配置不完整') | Out-Null
        return
    }
    $values = @(
        $p1.Text.Trim(), $p2.Text.Trim(), $p3.Text.Trim(), $p4.Text.Trim(),
        $f1.Text.Trim(), $f2.Text.Trim(), $f3.Text.Trim(), $f4.Text.Trim()
    )
    if (@($values | Where-Object { $_ -match '[\r\n]' }).Count) {
        [Windows.Forms.MessageBox]::Show('配置值必须是单行。', '配置无效') | Out-Null
        return
    }
    foreach ($url in @($values[2], $values[6])) {
        $uri = $null
        if (-not [uri]::TryCreate($url, [UriKind]::Absolute, [ref]$uri) -or $uri.Scheme -notin @('https', 'http')) {
            [Windows.Forms.MessageBox]::Show('Base URL 必须是有效的 http 或 https URL。', 'URL 无效') | Out-Null
            return
        }
    }

    $names = @(
        'MODEL_PROVIDER', 'MODEL_NAME', 'MODEL_BASE_URL', 'MODEL_API_KEY',
        'FALLBACK_MODEL_PROVIDER', 'FALLBACK_MODEL_NAME', 'FALLBACK_MODEL_BASE_URL', 'FALLBACK_MODEL_API_KEY'
    )
    $old = if (Test-Path -LiteralPath $envPath) { [IO.File]::ReadAllText($envPath) } else { '' }
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

    $tmp = Join-Path ([IO.Path]::GetDirectoryName($envPath)) ('.env.codex-' + [guid]::NewGuid().ToString('N') + '.tmp')
    try {
        [IO.File]::WriteAllText($tmp, ([string]::Join([Environment]::NewLine, $kept) + [Environment]::NewLine), (New-Object Text.UTF8Encoding($false)))
        if (Test-Path -LiteralPath $envPath) { [IO.File]::Replace($tmp, $envPath, $null) }
        else { [IO.File]::Move($tmp, $envPath) }
        $form.Tag = 'saved'
        $form.DialogResult = [Windows.Forms.DialogResult]::OK
        $form.Close()
    } catch {
        [Windows.Forms.MessageBox]::Show('写入失败，原配置已保留；请检查文件权限后重试。', '保存失败') | Out-Null
    } finally {
        if (Test-Path -LiteralPath $tmp) { Remove-Item -LiteralPath $tmp -Force -ErrorAction SilentlyContinue }
    }
})

$result = $form.ShowDialog()
if ($result -eq [Windows.Forms.DialogResult]::OK -and $form.Tag -eq 'saved') { exit 0 }
exit 2
