"""Real TLS + GNS smoke test: direct or forced coturn, optionally an ADB client.

Uses temporary server/account directories and removes its container/device files.
No game files, Steam account, or installed Android app are used.
"""
import argparse
import os
from pathlib import Path
import secrets
import socket
import subprocess as sp
import tempfile
import time

COTURN = "coturn/coturn:4.6.3@sha256:71c3c990283385567f11794ee692e3a47b66fd9b0bb39e42afbe776e331dd888"


def free_port(kind=socket.SOCK_STREAM):
    with socket.socket(type=kind) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", required=True, type=Path)
    parser.add_argument("--probe", required=True, type=Path)
    parser.add_argument("--relay", action="store_true")
    parser.add_argument("--android-probe", type=Path)
    parser.add_argument("--serial")
    parser.add_argument("--guest-probe", type=Path, help="Windows probe executable, run using Wine")
    args = parser.parse_args()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.connect(("192.0.2.1", 9))
        ip = s.getsockname()[0]
    port, turn_port = free_port(), free_port(socket.SOCK_DGRAM)
    container = "ttt2-crossplay-test-" + secrets.token_hex(5)
    remote = "/data/local/tmp/" + container
    adb = ["adb"] + (["-s", args.serial] if args.serial else [])
    processes = []
    logs = []
    try:
        with tempfile.TemporaryDirectory(prefix="ttt2-crossplay-") as temp:
            root = Path(temp)
            secret = secrets.token_hex(32)
            config = f'''name = "Crossplay test"
crossplay = true
port = {port}
max_connections_per_ip = 16
data_dir = "data"
'''
            if args.relay:
                config += f'stun_server = "{ip}:{turn_port}"\nturn_server = "{ip}:{turn_port}"\nturn_secret = "{secret}"\n'
                sp.run(["docker", "run", "--detach", "--rm", "--name", container, "--network", "host", COTURN,
                        "-n", "--listening-ip="+ip, "--relay-ip="+ip, "--listening-port="+str(turn_port),
                        "--min-port=38430", "--max-port=38450", "--realm=ttt2-test", "--use-auth-secret",
                        "--static-auth-secret="+secret, "--no-tls", "--no-dtls", "--no-cli", "--log-file=stdout"],
                       check=True, stdout=sp.DEVNULL)
            path = root / "server.toml"
            path.write_text(config)
            path.chmod(0o600)
            log = open(root / "server.log", "w+"); logs.append(log)
            server = sp.Popen([str(args.server.resolve()), "-config", str(path)], stdin=sp.DEVNULL, stdout=log, stderr=sp.STDOUT)
            processes.append(server)
            for _ in range(100):
                if server.poll() is not None:
                    log.seek(0); raise RuntimeError(log.read())
                try:
                    with socket.create_connection(("127.0.0.1", port), .1):
                        break
                except OSError:
                    time.sleep(.05)
            else:
                raise RuntimeError("Server did not become ready")
            if args.relay:
                time.sleep(.5)
            if args.android_probe:
                sp.run(adb + ["shell", "mkdir", "-p", remote], check=True)
                sp.run(adb + ["push", str(args.android_probe.resolve()), remote+"/probe"], check=True, stdout=sp.DEVNULL)
                sp.run(adb + ["shell", "chmod", "700", remote+"/probe"], check=True)
            peers = []
            for role in ("host", "guest"):
                env = os.environ.copy()
                env["TTT2_SAVE_ROOT"] = str(root / role)
                if args.relay:
                    env["TTT2_CROSSPLAY_FORCE_RELAY"] = "1"
                else:
                    env.pop("TTT2_CROSSPLAY_FORCE_RELAY", None)
                address = f"{ip}:{port}"
                command = [str(args.probe.resolve()), address, role]
                if role == "guest" and args.android_probe:
                    command = adb + ["shell", "env", "TTT2_SAVE_ROOT="+remote,
                                     "TTT2_CROSSPLAY_FORCE_RELAY=1"] if args.relay else adb + ["shell", "env", "TTT2_SAVE_ROOT="+remote]
                    command += [remote+"/probe", address, role]
                elif role == "guest" and args.guest_probe:
                    command = ["wine", str(args.guest_probe.resolve()), address, role]
                    env["WINEDEBUG"] = "-all"
                    env["TTT2_SAVE_ROOT"] = "Z:" + str(root / role)
                peerlog = open(root / (role+".log"), "w+"); logs.append(peerlog)
                peer = sp.Popen(command, env=env, stdout=peerlog, stderr=sp.STDOUT)
                processes.append(peer); peers.append((peer, peerlog))
                if role == "host":
                    time.sleep(.5)
            failed = False
            for peer, peerlog in peers:
                status = peer.wait(timeout=45)
                peerlog.seek(0); print(peerlog.read(), end="")
                failed |= status != 0
            if failed:
                raise RuntimeError("Crossplay probe failed")
            print("PASS", "forced relay" if args.relay else "direct", "TLS admission, persisted identity, reliable ordering and both packet channels")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except sp.TimeoutExpired:
                    process.kill(); process.wait()
        for log in logs:
            log.close()
        if args.relay:
            sp.run(["docker", "stop", container], stdout=sp.DEVNULL, stderr=sp.DEVNULL)
        if args.android_probe:
            sp.run(adb + ["shell", "rm", "-rf", remote], stdout=sp.DEVNULL)


if __name__ == "__main__":
    main()
