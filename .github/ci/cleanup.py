"""Remove private inputs, intermediate game sources and signing data."""
import os
from pathlib import Path
import shutil

for directory in ('private-inputs', 'game/re/ppc', 'game/re/decrypted'):
    shutil.rmtree(directory, ignore_errors=True)
Path('game/re/recomp/Default_patched_tu3.xex').unlink(missing_ok=True)
for directory in ('android-signing', 'ttt2-build'):
    shutil.rmtree(Path(os.environ['RUNNER_TEMP']) / directory, ignore_errors=True)
