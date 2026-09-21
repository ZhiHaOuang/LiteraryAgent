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

from .providers import PROVIDERS, normalize_profile, profile_settings, provider_id, validate_model


def checked_name(name: str) -> str:
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", name):
        raise ValueError("Names must start with a letter or digit and contain 1-64 letters, digits, dots, underscores or hyphens")
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
            if not isinstance(data, dict) or not isinstance(data.get("profiles"), dict):
                raise ValueError
            if data.get("version", 1) not in {1, 2}:
                raise ValueError
            if data.get("active") is not None and data["active"] not in data["profiles"]:
                raise ValueError
            for name, profile in data["profiles"].items():
                checked_name(name)
                data["profiles"][name] = normalize_profile(profile)
        except (ValueError, KeyError, TypeError):
            raise ValueError("Invalid LG credential store; contents suppressed") from None
        yield data, path


def read_profiles(environment: str) -> dict:
    if not os.path.lexists(environment_dir(environment) / "credentials.json"):
        return {"active": None, "profiles": {}}
    with _store(environment) as (data, _):
        return data


def save_profile(environment: str, name: str, key: str | None = None, *, replace: bool = False,
                 provider: str | None = None, model: str | None = None,
                 base_url: str | None = None) -> None:
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
            previous = data["profiles"].get(name, {})
            if previous.get("auth_mode") == "chatgpt":
                raise ValueError("Subscription profile: use auth login, not API key replacement")
            selected = provider_id(provider or previous.get("provider") or "deepseek")
            same_provider = selected == previous.get("provider")
            settings = profile_settings(selected, model or (previous.get("model") if same_provider else None),
                                        base_url or (previous.get("base_url") if same_provider else None))
            data["profiles"][name] = normalize_profile({**settings, "key": key.strip()})
        if key is None and model is not None:
            data["profiles"][name]["model"] = validate_model(model)
        data["version"] = 2
        data["active"] = name
        _write_store(data, path)


def save_subscription(environment: str, name: str, model: str = "") -> None:
    checked_name(name)
    profile = normalize_profile({"provider": "codex", "auth_mode": "chatgpt", "model": model})
    with _store(environment) as (data, path):
        previous = data["profiles"].get(name)
        if previous and previous.get("auth_mode") != "chatgpt":
            raise ValueError("Name already belongs to an API profile; choose a separate subscription name")
        data["profiles"][name] = profile
        data.update(version=2, active=name)
        _write_store(data, path)


def _write_store(data: dict, path: Path) -> None:
    # Caller holds the stable directory lock across this atomic replacement.
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
    auth = sub.add_parser("auth", help="Manage API profiles and explicit ChatGPT subscription login.")
    commands = auth.add_subparsers(dest="auth_command")
    add = commands.add_parser("add", help="Save a provider key and activate its model profile.")
    add.add_argument("name")
    add.add_argument("--provider", choices=tuple(PROVIDERS))
    add.add_argument("--model", help="Model ID available to your account.")
    add.add_argument("--base-url", help="Explicit compatible endpoint; no automatic failover.")
    add.add_argument("--replace", action="store_true")
    add.add_argument("--key-env", help="Read the key from a named environment variable, not an argument.")
    use = commands.add_parser("use", help="Switch the active saved key.")
    use.add_argument("name")
    use.add_argument("--model", help="Update the selected profile's model.")
    model = commands.add_parser("model", help="Set the active profile's model without re-entering its key.")
    model.add_argument("model")
    commands.add_parser("providers", help="List supported protocols and default endpoints.")
    login = commands.add_parser("login", help="Explicit official Codex ChatGPT subscription login (no API key).")
    login.add_argument("name", nargs="?", default="gpt")
    login.add_argument("--model", default="", help="Optional subscription model ID; otherwise use Codex's account default.")
    login.add_argument("--browser", action="store_true", help="Use browser callback instead of device code.")
    commands.add_parser("status", help="Check the selected credential type; do not send a model request.")
    commands.add_parser("list", help="List profile names only; never show keys.")


def dispatch_auth(args, environment: str) -> int:
    if args.auth_command == "login":
        from .subscription_auth import login_subscription
        return login_subscription(environment, args.name, Path(args.cwd).resolve() if args.cwd else Path.cwd(), model=args.model, browser=args.browser)
    if args.auth_command == "status":
        from .config import load_config
        from .subscription_auth import subscription_status
        config = load_config(Path(args.cwd).resolve() if args.cwd else None, environment=environment)
        if config.auth_mode == "chatgpt":
            ok, message = subscription_status(config)
            print(message)
            return 0 if ok else 1
        print(f"{environment}/{config.profile_name or '(environment)'}: {'API key configured (not probed)' if config.api_key else 'not configured'}")
        return 0 if config.api_key else 1
    if args.auth_command is None:
        from .auth_ui import manage_auth
        return manage_auth(environment, Path(args.cwd).resolve() if args.cwd else None)
    if args.auth_command == "providers":
        for name, preset in PROVIDERS.items():
            print(f"{name:24} {preset.protocol:10} {preset.base_url or '(explicit URL required)'}")
        print("codex                    subscription  literary auth login gpt")
        return 0
    if args.auth_command == "add":
        checked_name(args.name)
        if args.name in {"gpt", "chatgpt"} and not args.provider and sys.stdin.isatty() and sys.stdout.isatty() and not args.key_env:
            from .auth_ui import radiolist_dialog
            selected = radiolist_dialog(title="GPT authentication", text="Choose access type",
                                        values=[("codex", "ChatGPT subscription (official Codex login)"),
                                                ("openai", "OpenAI API (usage-based billing)"),
                                                ("responses-compatible", "Existing Responses / CPA proxy")]).run()
            if selected is None:
                return 0
            if selected == "codex":
                if args.base_url:
                    raise ValueError("Official subscription login does not accept a proxy Base URL")
                from .subscription_auth import login_subscription
                return login_subscription(environment, args.name, Path(args.cwd).resolve() if args.cwd else Path.cwd(), model=args.model or "")
            args.provider = selected
        if args.name in {"gpt", "chatgpt", "cpa"} and not args.provider:
            raise ValueError("Subscription: use literary auth login gpt. For paid API access specify --provider openai; for an existing proxy specify --provider responses-compatible. These are separate authentication flows.")
        previous = read_profiles(environment)["profiles"].get(args.name, {})
        if previous.get("auth_mode") == "chatgpt":
            raise ValueError("Subscription profile: use auth login, not API key replacement")
        provider = args.provider or previous.get("provider") or (args.name if args.name in PROVIDERS else "deepseek")
        model = args.model or (previous.get("model") if provider == previous.get("provider") else None) or PROVIDERS[provider].default_model
        base_url = args.base_url or (previous.get("base_url") if provider == previous.get("provider") else None) or PROVIDERS[provider].base_url
        if not model and sys.stdin.isatty() and not args.key_env:
            model = input(f"{PROVIDERS[provider].name} model ID: ").strip()
        if not base_url and sys.stdin.isatty() and not args.key_env:
            base_url = input("Provider Base URL: ").strip()
        settings = profile_settings(provider, model, base_url)
        if previous and not args.replace:
            raise ValueError("Profile exists; use --replace to explicitly update its key")
        print(f"Provider: {settings['provider']} | Model: {settings['model']} | Endpoint: {settings['base_url']}")
        if args.key_env:
            key = os.environ.get(args.key_env, "")
        else:
            if not sys.stdin.isatty():
                raise ValueError("Hidden key input needs a terminal; alternatively use --key-env VARIABLE")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                key = getpass.getpass(f"{PROVIDERS[provider].name} API key (hidden): ")
        save_profile(environment, args.name, key, replace=args.replace, provider=provider, model=model, base_url=base_url)
        print(f"Saved and activated: {environment}/{args.name}")
    elif args.auth_command == "use":
        save_profile(environment, args.name, model=args.model)
        print(f"Active: {environment}/{args.name}")
    elif args.auth_command == "model":
        name = read_profiles(environment)["active"]
        if not name:
            raise ValueError("No active profile; run literary auth add first")
        save_profile(environment, name, model=args.model)
        print(f"Model updated: {environment}/{name}")
    else:
        data = read_profiles(environment)
        print(f"LG environment: {environment}")
        for name in sorted(data["profiles"]):
            profile = data["profiles"][name]
            print(f"{'*' if name == data['active'] else ' '} {name} | {profile['provider']} | {profile['model']} | {profile['protocol']} | {profile['base_url']}")
        if not data["profiles"]:
            print("No saved keys. Run auth add NAME.")
    return 0
