"""Materialize existing signing credentials without printing them."""
import base64
import os
from pathlib import Path

key = os.environ.get('ANDROID_RELEASE_KEYSTORE', '')
password = os.environ.get('ANDROID_RELEASE_PASSWORD', '')
if not key or not password:
    raise SystemExit('Configure both Android release signing secrets before building an APK')
os.umask(0o077)
directory = Path(os.environ['RUNNER_TEMP']) / 'android-signing'
directory.mkdir(exist_ok=True)
(directory / 'release.jks').write_bytes(base64.b64decode(key, validate=True))
(directory / 'password').write_text(password.rstrip('\r\n') + '\n')
