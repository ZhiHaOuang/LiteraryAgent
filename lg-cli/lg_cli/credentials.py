"""LG-only credential spaces. Secrets never enter project configuration."""
from __future__ import annotations

import fcntl
import getpass
import json
import os
import re
import stat
import sys
import tempfile
import warnings
from contextlib import ExitStack, contextmanager
from pathlib import Path


def checked_name(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", name):
        raise ValueError("Use 1-64 letters, digits, underscores or hyphens for names")
    return name


def environment_dir(environment: str) -> Path:
    return Path.home() / ".literarygiant" / "environments" / checked_name(environment)


@contextmanager
def _store(environment: str):
    directory = environment_dir(environment)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink():
        raise ValueError("Credential directory must not be a symlink")
    directory.chmod(0o700)
    path = directory / "credentials.json"
    with ExitStack() as stack:
        lock_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        stack.callback(os.close, lock_fd)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        stream = stack.enter_context(os.fdopen(fd, "r+", encoding="utf-8"))
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_nlink != 1:
            raise ValueError("Unsafe credential file ownership or type")
        os.fchmod(stream.fileno(), 0o600)
        raw = stream.read()
        try:
            data = json.loads(raw) if raw else {"active": None, "profiles": {}}
            assert isinstance(data, dict) and isinstance(data["profiles"], dict)
            assert data.get("active") is None or data["active"] in data["profiles"]
            for name, profile in data["profiles"].items():
                checked_name(name)
                assert isinstance(profile, dict)
                assert isinstance(profile["key"], str) and profile["key"].strip()
                assert profile["provider"] == "deepseek-anthropic"
        except (ValueError, KeyError, TypeError, AssertionError):
            raise ValueError("Invalid LG credential store; contents suppressed") from None
        yield data, path


def read_profiles(environment: str) -> dict:
    if not os.path.lexists(environment_dir(environment) / "credentials.json"):
        return {"active": None, "profiles": {}}
    with _store(environment) as (data, _):
        return data


def save_profile(environment: str, name: str, key: str | None = None, *, replace: bool = False) -> None:
    checked_name(name)
    if key is not None and (not key.strip() or any(c.isspace() for c in key.strip())):
        raise ValueError("API key must be nonempty and contain no whitespace")
    with _store(environment) as (data, path):
        if key is None:
            if name not in data["profiles"]:
                raise ValueError("Profile not found; add it first")
        else:
            if name in data["profiles"] and not replace:
                raise ValueError("Profile exists; use --replace to explicitly update its key")
            data["profiles"][name] = {"provider": "deepseek-anthropic", "key": key.strip()}
        data["active"] = name
        # Stable directory lock covers atomic replacement and concurrent readers.
        with tempfile.TemporaryDirectory(dir=path.parent, prefix=".credentials-") as staging:
            staged = Path(staging) / "credentials.json"
            fd = os.open(staged, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(data, stream)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(staged, path)
        directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def add_auth_parser(sub) -> None:
    auth = sub.add_parser("auth", help="Manage private LG API key profiles (no account login).")
    commands = auth.add_subparsers(dest="auth_command", required=True)
    add = commands.add_parser("add", help="Save a DeepSeek key and activate it.")
    add.add_argument("name")
    add.add_argument("--replace", action="store_true")
    add.add_argument("--key-env", help="Read the key from a named environment variable, not an argument.")
    use = commands.add_parser("use", help="Switch the active saved key.")
    use.add_argument("name")
    commands.add_parser("list", help="List profile names only; never show keys.")


def dispatch_auth(args, environment: str) -> int:
    if args.auth_command == "add":
        checked_name(args.name)
        if args.key_env:
            key = os.environ.get(args.key_env, "")
        else:
            if not sys.stdin.isatty():
                raise ValueError("Hidden key input needs a terminal; alternatively use --key-env VARIABLE")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass("DeepSeek API key (hidden): ")
        save_profile(environment, args.name, key, replace=args.replace)
        print(f"Saved and activated: {environment}/{args.name}")
    elif args.auth_command == "use":
        save_profile(environment, args.name)
        print(f"Active: {environment}/{args.name}")
    else:
        data = read_profiles(environment)
        print(f"LG environment: {environment}")
        for name in sorted(data["profiles"]):
            print(f"{'*' if name == data['active'] else ' '} {name}")
        if not data["profiles"]:
            print("No saved keys. Run auth add NAME.")
    return 0
