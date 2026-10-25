"""Resolve one source revision for all jobs; no private data is needed here."""
import json
import os
from pathlib import Path
import re
import subprocess

request = json.loads(Path(__file__).with_name('request.json').read_text())
if os.environ.get('SOURCE_REF'):
    request['source_ref'] = os.environ['SOURCE_REF']
if os.environ.get('BUILD_TARGETS'):
    request['targets'] = os.environ['BUILD_TARGETS'].split(',')
targets = request['targets']
if not isinstance(targets, list) or not targets or set(targets) - {'linux', 'windows', 'android', 'server'}:
    raise SystemExit('Invalid build targets')
ref = request['source_ref']
if not isinstance(ref, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._/-]*', ref):
    raise SystemExit('Invalid source ref')
if not isinstance(request.get('steam'), bool):
    raise SystemExit('steam must be a boolean')
repository = os.environ['GITHUB_REPOSITORY']
commit = json.loads(subprocess.check_output(
    ['gh', 'api', f'repos/{repository}/commits/{ref}'], text=True))['sha']
if not re.fullmatch(r'[0-9a-f]{40}', commit):
    raise SystemExit('Invalid resolved commit')
private = os.environ.get('PRIVATE_INPUTS_REPOSITORY', '')
with open(os.environ['GITHUB_OUTPUT'], 'a') as output:
    print(f'sha={commit}', file=output)
    print(f'label={re.sub(r"[^A-Za-z0-9._-]", "-", ref)[:60]}-{commit[:12]}', file=output)
    print(f'steam={str(request["steam"]).lower()}', file=output)
    for target in ['linux', 'windows', 'android', 'server']:
        enabled = target in targets and (target == 'server' or bool(private))
        print(f'{target}={str(enabled).lower()}', file=output)
with open(os.environ['GITHUB_STEP_SUMMARY'], 'a') as summary:
    summary.write(f'Building source commit `{commit}`.\n\n')
    if not private and any(t != 'server' for t in targets):
        summary.write('Game builds are **not configured**: set PRIVATE_INPUTS_REPOSITORY and '
                      'the private-build-inputs environment credentials. Only the server can build.\n')
