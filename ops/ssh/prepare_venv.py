"""Prepare a reusable virtual environment on the deployment target."""

import fcntl
import hashlib
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import sysconfig
import tempfile


def environment_key(requirements):
    digest = hashlib.sha256()
    for value in (Path(sys.executable).resolve(), sys.version, sysconfig.get_platform(),
                  sysconfig.get_config_var("SOABI")):
        digest.update(str(value).encode() + b"\0")

    seen = set()

    def include(path):
        path = path.resolve()
        if path in seen:
            return
        seen.add(path)
        content = path.read_bytes()
        digest.update(os.fsencode(os.path.relpath(path, requirements.parent)) + b"\0")
        digest.update(hashlib.sha256(content).digest())
        for line in content.decode("utf-8-sig").replace("\\\n", "").splitlines():
            match = re.match(r"\s*(?:-[rc]\s*|--(?:requirement|constraint)(?:=|\s+))(.+)", line)
            if match:
                target = shlex.split(match[1], comments=True)[0]
                include(path.parent / target)

    include(requirements)
    return digest.hexdigest()


def prepare(release, cache):
    requirements = release / "ops/requirements-live.txt"
    environment = cache / environment_key(requirements)
    ready = environment / ".ready"
    link = release / ".venv"
    cache.mkdir(exist_ok=True)
    with (cache / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not ready.is_file():
            if environment.exists():
                shutil.rmtree(environment)
            subprocess.run([sys.executable, "-m", "venv", environment], check=True)
            subprocess.run(
                [environment / "bin/python", "-m", "pip", "install", "-r", requirements],
                check=True,
            )
            ready.touch()
        else:
            print(f"Reusing Python environment {environment.name[:12]}", flush=True)
        if link.is_symlink() and link.readlink() == environment:
            return
        if link.is_dir() and not link.is_symlink():
            # An interrupted deployment with the former per-release layout.
            shutil.rmtree(link)
        with tempfile.TemporaryDirectory(dir=release) as staging:
            pending = Path(staging) / ".venv"
            pending.symlink_to(environment, target_is_directory=True)
            pending.replace(link)


if __name__ == "__main__":
    prepare(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve())
