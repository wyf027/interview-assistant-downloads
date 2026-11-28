param([switch]$Layout)
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.DataWriter, Windows.Storage.Streams, ContentType=WindowsRuntime]
$null = [Windows.Storage.Streams.InMemoryRandomAccessStream, Windows.Storage.Streams, ContentType=WindowsRuntime]
$bridge = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and $_.IsGenericMethod -and $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
} | Select-Object -First 1
function Await-Result($operation, $resultType) {
    $task = $bridge.MakeGenericMethod($resultType).Invoke($null, @($operation))
    $task.GetAwaiter().GetResult()
}
try {
    $stream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
    $writer = [Windows.Storage.Streams.DataWriter]::new($stream)
    $writer.WriteBytes([Convert]::FromBase64String([Console]::In.ReadToEnd()))
    $null = Await-Result ($writer.StoreAsync()) ([uint32])
    $null = $writer.DetachStream()
    $stream.Seek(0)
    $decoder = Await-Result ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
    $bitmap = Await-Result ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
    $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
    if (!$engine) { throw 'Windows OCR language pack is missing.' }
    $result = Await-Result ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
    if ($Layout) {
        $boxes = foreach ($line in $result.Lines) {
            $words = @($line.Words)
            if (!$words.Count) { continue }
            $left = [double]::PositiveInfinity
            $top = [double]::PositiveInfinity
            $bottom = 0
            foreach ($word in $words) {
                $r = $word.BoundingRect
                $left = [Math]::Min($left,$r.X)
                $top = [Math]::Min($top,$r.Y)
                $bottom = [Math]::Max($bottom,$r.Y+$r.Height)
            }
            @{x=$left; center=($top+$bottom)/2; height=$bottom-$top; text=$line.Text}
        }
        [Console]::WriteLine((ConvertTo-Json -InputObject @($boxes) -Compress))
    } else {
        foreach ($line in $result.Lines) { [Console]::WriteLine($line.Text) }
    }
} catch {
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 1
}
