[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$videoRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..'))
$videoPython = Join-Path $videoRoot 'runtime\video-asr\Scripts\python.exe'
$videoUv = Join-Path $videoRoot '.bootstrap\Scripts\uv.exe'
if (-not (Test-Path -LiteralPath $videoUv)) { throw 'Run the normal project installer first.' }
if (-not (Test-Path -LiteralPath $videoPython)) {
    & (Join-Path $videoRoot '.venv\Scripts\python.exe') -m venv (Join-Path $videoRoot 'runtime\video-asr')
    if ($LASTEXITCODE -ne 0) { throw 'Cannot create the isolated speech environment.' }
}
& $videoUv pip install --python $videoPython 'faster-whisper==1.2.1'
if ($LASTEXITCODE -ne 0) { throw 'Cannot prepare speech dependencies.' }
$videoPrepare = @'
import sys
from huggingface_hub import HfApi, snapshot_download
repo = 'Systran/faster-whisper-tiny'
revision = 'd90ca5fe260221311c53c58e660288d3deb8d356'
names = ['model.bin', 'config.json', 'tokenizer.json', 'vocabulary.txt']
info = HfApi().model_info(repo, revision=revision, files_metadata=True)
size = sum(row.size or 0 for row in info.siblings if row.rfilename in names)
assert 0 < size < 100_000_000, 'Model exceeds the configured small-model allowance'
snapshot_download(repo, revision=revision, allow_patterns=names, local_dir=sys.argv[1])
print('Prepared local speech model:', size, 'bytes')
'@
& $videoPython -I -X utf8 -c $videoPrepare (Join-Path $videoRoot 'runtime\video-asr\models\tiny')
if ($LASTEXITCODE -ne 0) { throw 'Model preparation failed; runtime will not download it automatically.' }
Write-Output "Speech Python: $videoPython"
Write-Output ('Model directory: ' + (Join-Path $videoRoot 'runtime\video-asr\models\tiny'))
