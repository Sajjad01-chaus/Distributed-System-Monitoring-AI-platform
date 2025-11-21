$lines = Get-Content app.py
$counter = 1
$newLines = @()

foreach ($line in $lines) {
    if ($line -match "st\.plotly_chart\(fig, width='stretch'\)") {
        $newLine = $line -replace "st\.plotly_chart\(fig, width='stretch'\)", "st.plotly_chart(fig, width='stretch', key='chart_$counter')"
        $newLines += $newLine
        $counter++
    } else {
        $newLines += $line
    }
}

$newLines | Set-Content app.py
Write-Host "Fixed $($counter-1) charts!" -ForegroundColor Green