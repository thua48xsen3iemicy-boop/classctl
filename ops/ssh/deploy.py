#!/usr/bin/env python3
"""Deploy a committed classctl revision to an explicitly listed classroom."""
import argparse
import fcntl
from functools import lru_cache
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
from urllib.parse import urlsplit

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]


def run(*args, data=None, stdin=None):
    return subprocess.run(args, input=data, stdin=stdin, stdout=subprocess.PIPE,
                          check=True).stdout


def remote(host, *args, **kw):
    if "@" not in host:
        host = "root@" + host
    return run("ssh", "-oBatchMode=yes", "-oStrictHostKeyChecking=yes", host,
               shlex.join(map(str, args)), **kw)


@lru_cache
def privilege(host):
    return () if remote(host, "id", "-u").strip() == b"0" else ("sudo", "-n")


def worker(host, *args, pip_proxy=None, pip_index_url=None):
    script = (HERE / "host.sh").read_bytes()
    # Send URLs through SSH stdin so credentials stay out of command arguments.
    settings = {"classctl_pip_proxy": pip_proxy, "classctl_pip_index_url": pip_index_url}
    if pip_index_url is not None:
        index = urlsplit(pip_index_url)
        if index.scheme == "http":
            settings["classctl_pip_trusted_host"] = index.netloc.rsplit("@", 1)[-1]
    prefix = "".join(f"{key}={shlex.quote(value)}\n"
                     for key, value in settings.items() if value is not None)
    script = prefix.encode() + script
    return remote(host, *privilege(host), "bash", "-s", "--", *args, data=script)


def upload(host, path, data, mode="0644"):
    remote(host, *privilege(host), "python3", "-c", """
import os, sys, tempfile
path, mode = sys.argv[1:]
with tempfile.NamedTemporaryFile(dir=os.path.dirname(path), delete=False) as f:
    f.write(sys.stdin.buffer.read())
    os.fchmod(f.fileno(), int(mode, 8))
os.replace(f.name, path)
""", path, mode, data=data)


def token(value, pattern):
    if not isinstance(value, str) or not re.fullmatch(pattern, value):
        raise ValueError(f"Invalid inventory value: {value!r}")


def validate_pip_url(field, value):
    if value is None:
        return
    message = f"{field} must be an HTTP or HTTPS URL, or null"
    if not isinstance(value, str) or any(c.isspace() or not c.isprintable() for c in value):
        raise ValueError(message)
    try:
        url = urlsplit(value)
        valid = url.scheme in ("http", "https") and url.hostname and url.port != 0
        if field == "pip_index_url" and url.scheme == "http" and "*" in url.netloc.rsplit("@", 1)[-1]:
            raise ValueError(message)
    except ValueError:
        raise ValueError(message) from None
    if not valid:
        raise ValueError(message)


def validate(cfg):
    for field in ("pip_proxy", "pip_index_url"):
        validate_pip_url(field, cfg.get(field))
    name = r"[A-Za-z0-9][A-Za-z0-9._-]*"
    target = rf"(?:{name}@)?{name}"
    token(cfg["coordinator"], target)
    ident = r"[A-Za-z0-9][A-Za-z0-9._-]{0,95}"
    token(cfg["room"], ident)
    token(cfg["dns"], name)
    if "teachers" in cfg:
        raise ValueError("Use the dedicated teacher PC as coordinator and set room rights")
    for key in ("port", "ui_port"):
        if type(cfg[key]) is not int or not 0 < cfg[key] < 65536:
            raise ValueError(f"Invalid {key}")
    if cfg["port"] == cfg["ui_port"]:
        raise ValueError("Agent and UI ports must differ")
    if sorted(cfg["rights"]) not in (["view"], ["control", "view"]):
        raise ValueError("Room rights must be view or view + control")
    if not cfg["students"]:
        raise ValueError("Student nodes are required")
    nodes = set()
    hosts = {cfg["coordinator"].removeprefix("root@")}
    for s in cfg["students"]:
        token(s["node"], ident)
        token(s["ssh"], target)
        host = s["ssh"].removeprefix("root@")
        if s["node"] in nodes or host in hosts:
            raise ValueError("Duplicate student node or SSH target")
        hosts.add(host)
        nodes.add(s["node"])


def ca(state):
    key, cert = state / "ca.key", state / "ca.pem"
    if not key.exists() and not cert.exists():
        run("openssl", "req", "-x509", "-newkey", "rsa:3072", "-nodes",
            "-days", "3650", "-subj", "/CN=classctl classroom CA",
            "-addext", "basicConstraints=critical,CA:TRUE,pathlen:0",
            "-addext", "keyUsage=critical,keyCertSign,cRLSign",
            "-keyout", key, "-out", cert)
    if run("openssl", "pkey", "-in", key, "-pubout") != run(
            "openssl", "x509", "-in", cert, "-pubkey", "-noout"):
        raise ValueError("CA key mismatch")
    run("openssl", "x509", "-in", cert, "-checkend", str(31 * 86400), "-noout")
    return cert


def identity(host, role, label, state, dns):
    folder = "/etc/classctl/" + label
    csr = worker(host, "csr", folder, label)
    usage = "serverAuth" if role == "server" else "clientAuth"
    ext = f"basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage={usage}\n"
    if role == "server":
        try:
            ipaddress.ip_address(dns)
            kind = "IP"
        except ValueError:
            kind = "DNS"
        ext += f"subjectAltName={kind}:{dns}\n"
    pub = run("openssl", "req", "-pubkey", "-noout", data=csr)
    cache = state / (hashlib.sha256(pub + ext.encode() + label.encode()).hexdigest() + ".pem")
    if not cache.exists() or subprocess.call(
            ["openssl", "x509", "-in", cache, "-checkend", str(30 * 86400), "-noout"],
            stdout=subprocess.DEVNULL):
        with tempfile.NamedTemporaryFile(dir=state) as extensions:
            extensions.write(ext.encode())
            extensions.flush()
            pem = run("openssl", "x509", "-req", "-CA", state / "ca.pem",
                      "-CAkey", state / "ca.key", "-set_serial", str(int.from_bytes(os.urandom(16), "big")),
                      "-days", "365", "-sha256", "-extfile", extensions.name, data=csr)
        cache.with_suffix(".tmp").write_bytes(pem)
        cache.with_suffix(".tmp").replace(cache)
    run("openssl", "verify", "-CAfile", state / "ca.pem", cache)
    upload(host, folder + "/cert.pem", cache.read_bytes())
    upload(host, folder + "/ca.pem", (state / "ca.pem").read_bytes())
    return hashlib.sha256(run("openssl", "x509", "-in", cache, "-outform", "DER")).hexdigest()


def deploy(cfg, state, tmp):
    server, dns, port = cfg["coordinator"], cfg["dns"], cfg["port"]
    rev = run("git", "-C", ROOT, "rev-parse", "HEAD").decode().strip()
    bundle = tmp / "project.bundle"
    run("git", "-C", ROOT, "bundle", "create", bundle, "HEAD")
    ca(state)
    roles = {s["ssh"]: "student" for s in cfg["students"]}
    roles[server] = "server"
    for host, role in roles.items():
        print("Preparing", host, flush=True)
        worker(host, "prepare", role)
        with bundle.open("rb") as src:
            transfer_bundle(host, rev, src)
        worker(host, "release", rev,
               pip_proxy=cfg.get("pip_proxy"), pip_index_url=cfg.get("pip_index_url"))
    policy = {"teachers": {"local": {"rooms": {cfg["room"]: cfg["rights"]}}}, "nodes": {}}
    seats = [dict(seatId=f"desk-{i}", row=1, column=i, nodeId=s["node"])
             for i, s in enumerate(cfg["students"], 1)]
    room = dict(version=1, kind="room", roomId=cfg["room"], seats=seats)
    identity(server, "server", "server", state, dns)
    for s in cfg["students"]:
        host, node = s["ssh"], s["node"]
        fp = identity(host, "node", "node", state, dns)
        if fp in policy["nodes"]:
            raise ValueError("Two nodes share a certificate")
        policy["nodes"][fp] = node
    upload(server, "/etc/classctl/room.json", json.dumps(room).encode())
    worker(server, "inventory", rev)
    upload(server, "/etc/classctl/policy.json", json.dumps(policy).encode())
    worker(server, "server", rev, port, cfg["ui_port"])
    url = f"https://{dns}:{port}"
    for s in cfg["students"]:
        worker(s["ssh"], "student", rev, s["node"], url)
    ui = f"http://127.0.0.1:{cfg['ui_port']}"
    worker(server, "check", ui, *(s["node"] for s in cfg["students"]))
    print(f"Teacher portal on {server}: {ui}/")


def transfer_bundle(host, rev, src):
    remote(host, *privilege(host), "sh", "-c",
           f"cat > /var/lib/classctl/{rev}.bundle", stdin=src)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inventory", type=Path)
    parser.add_argument("--state", type=Path, default=Path.home() / ".local/state/classctl")
    parser.add_argument("--check", action="store_true", help="validate inventory offline")
    args = parser.parse_args()
    cfg = json.loads(args.inventory.read_text())
    validate(cfg)
    if args.check:
        print("Inventory OK")
        return
    os.umask(0o077)
    args.state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (args.state / "lock").open("a") as lock, tempfile.TemporaryDirectory() as temp:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        deploy(cfg, args.state, Path(temp))


if __name__ == "__main__":
    main()
