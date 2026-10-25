"""Create private local/ configuration; never overwrites existing settings."""
import argparse
import ipaddress
from pathlib import Path
import re
import secrets

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("--hostname", required=True, help="Public DNS name for both services")
p.add_argument("--public-ip", required=True, type=ipaddress.IPv4Address)
p.add_argument("--bind-ip", type=ipaddress.IPv4Address, default=ipaddress.IPv4Address("0.0.0.0"))
a = p.parse_args()
if not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,251}[A-Za-z0-9])?", a.hostname):
    p.error("Use a DNS hostname without a scheme, path or port")
root = Path(__file__).resolve().parent / "local"
root.mkdir(mode=0o700, exist_ok=True)
if any(root.iterdir()):
    p.error("local/ already contains settings; edit them directly")
secret = secrets.token_hex(32)
(root / "server.toml").write_text(f'''name = "TTT2 Cross-platform Server"
crossplay = true
port = 28200
data_dir = "data"
max_players = 64
stun_server = "{a.hostname}:3478"
turn_server = "{a.hostname}:3478"
turn_secret = "{secret}"
# Optional: verify Steam bindings for Recently Played With.
steam_web_api_key = ""
trust_unverified_steam_ids = false
''')
external = str(a.public_ip) if a.bind_ip.is_unspecified or a.bind_ip == a.public_ip else f"{a.public_ip}/{a.bind_ip}"
(root / "turnserver.conf").write_text(f'''listening-port=3478
listening-ip={a.bind_ip}
external-ip={external}
min-port=49160
max-port=49260
realm={a.hostname}
server-name={a.hostname}
fingerprint
use-auth-secret
static-auth-secret={secret}
user-quota=8
total-quota=256
max-bps=1048576
bps-capacity=67108864
stale-nonce=600
no-tls
no-dtls
no-cli
no-multicast-peers
denied-peer-ip=0.0.0.0-0.255.255.255
denied-peer-ip=10.0.0.0-10.255.255.255
denied-peer-ip=127.0.0.0-127.255.255.255
denied-peer-ip=169.254.0.0-169.254.255.255
denied-peer-ip=172.16.0.0-172.31.255.255
denied-peer-ip=192.168.0.0-192.168.255.255
denied-peer-ip=::1
denied-peer-ip=fc00::-fdff:ffff:ffff:ffff:ffff:ffff:ffff:ffff
denied-peer-ip=fe80::-febf:ffff:ffff:ffff:ffff:ffff:ffff:ffff
log-file=stdout
simple-log
''')
for path in root.iterdir():
    path.chmod(0o600)
print(f"Created {root}. Set ownership to server uid 65532 and allow coturn to read its config (see README).")
