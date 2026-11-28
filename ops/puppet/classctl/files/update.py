#!/usr/bin/env python3
"""Apply a single host's classctl release during a Puppet run."""

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

BASE = Path("/opt/classctl")
CONFIG = Path("/etc/classctl")
STATE = Path("/var/lib/classctl")


def output(*command):
    return subprocess.check_output(command, text=True).strip()


def request(identity):
    if identity not in ("server", "node"):
        raise ValueError("Unknown certificate identity")
    folder = CONFIG / identity
    key = folder / "key.pem"
    with tempfile.TemporaryDirectory(dir=folder) as tmp:
        pem = Path(tmp) / "new.pem"
        if not key.exists():
            subprocess.run(["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt",
                            "rsa_keygen_bits:3072", "-out", pem], check=True)
            pem.chmod(0o600)
            pem.replace(key)
        subprocess.run(["openssl", "req", "-new", "-subj", "/CN=" + identity,
                        "-key", key, "-out", pem], check=True)
        pem.replace(folder / "request.pem")


def update(action, repository, branch, role, first, second):
    if action not in ("apply", "current") or role not in ("student", "server"):
        raise ValueError("Unknown action or role")
    subprocess.run(["git", "check-ref-format", "--branch", branch], check=True,
                   stdout=subprocess.DEVNULL)
    ref = "refs/heads/" + branch
    revision, remote_ref = output("git", "ls-remote", "--exit-code", "--refs",
                                  "--", repository, ref).split()
    if remote_ref != ref:
        raise ValueError("Unexpected branch returned by Git")
    release = BASE / revision
    identity = CONFIG / ("server" if role == "server" else "node")
    inputs = [identity / name for name in ("ca.pem", "cert.pem", "key.pem")]
    if role == "server":
        inputs.extend([CONFIG / "room.json", CONFIG / "policy.json"])
    desired = [revision, role, first, second,
               *(hashlib.sha256(path.read_bytes()).hexdigest() for path in inputs)]
    marker = STATE / ("puppet-" + role + ".json")
    if (release / ".ready").is_file() and marker.is_file() and marker.read_text() == json.dumps(desired):
        return True
    if action == "current":
        return False

    BASE.mkdir(mode=0o755, exist_ok=True)
    if not release.exists():
        # Publish the checkout only after Git has finished, including on retry.
        with tempfile.TemporaryDirectory(dir=BASE) as tmp:
            checkout = Path(tmp) / "checkout"
            subprocess.run(["git", "clone", "--single-branch", "--depth", "1", "--branch",
                            branch, "--", repository, checkout], check=True)
            if output("git", "-C", checkout, "rev-parse", "HEAD") != revision:
                raise ValueError("Branch changed during download, retry next run")
            checkout.rename(release)
    if output("git", "-C", release, "rev-parse", "HEAD") != revision:
        raise ValueError(f"Unexpected checkout in {release}")

    host = ["bash", release / "ops/ssh/host.sh"]
    subprocess.run([*host, "prepare", role], check=True)
    if role == "server":
        previous = output(*host, "existing-room")
        requested = json.loads((CONFIG / "room.json").read_text())["roomId"]
        if previous and previous != requested:
            raise ValueError(f"Existing room {previous} differs from requested {requested}")

    subprocess.run(["openssl", "verify", "-purpose",
                    "sslserver" if role == "server" else "sslclient",
                    "-CAfile", identity / "ca.pem", identity / "cert.pem"], check=True)
    if output("openssl", "x509", "-in", identity / "cert.pem", "-noout", "-pubkey") != output(
        "openssl", "pkey", "-in", identity / "key.pem", "-pubout"
    ):
        raise ValueError("Certificate does not match the local private key")

    subprocess.run([*host, "release", revision], check=True)
    marker.unlink(missing_ok=True)
    if role == "server":
        subprocess.run([*host, "inventory", revision], check=True)
    subprocess.run([*host, role, revision, first, second], check=True)
    if role == "server":
        subprocess.run([*host, "check", f"http://127.0.0.1:{second}"], check=True)
    with tempfile.NamedTemporaryFile(mode="w", dir=STATE, delete=False) as pending:
        pending.write(json.dumps(desired))
    os.replace(pending.name, marker)
    return True


if __name__ == "__main__":
    os.umask(0o022)
    os.environ["GIT_TERMINAL_PROMPT"] = "0"
    try:
        if sys.argv[1] == "request":
            request(sys.argv[2])
        else:
            sys.exit(0 if update(*sys.argv[1:]) else 1)
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        sys.exit(str(error))
