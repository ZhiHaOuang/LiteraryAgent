"""Plain ASCII auth prompts without colors, screen clearing or cursor menus."""

from __future__ import annotations

import getpass
import sys
import warnings
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .credentials import checked_name, read_profiles, save_profile
from .providers import PROVIDERS, profile_settings


class _Prompt:
    def __init__(self, action: Callable[[], Any]) -> None:
        self.action = action

    def run(self) -> Any:
        return self.action()


def radiolist_dialog(*, title: str, text: str, values: list, **_kwargs: Any) -> _Prompt:
    def ask():
        print(f"\n{title}")
        if text:
            print(text)
        for index, (_, label) in enumerate(values, 1):
            print(f"  {index}. {label}")
        print("  0. Back")
        while True:
            choice = input("> ").strip()
            if choice in {"", "0"}:
                return None
            if (
                choice.isascii()
                and choice.isdigit()
                and 1 <= int(choice) <= len(values)
            ):
                return values[int(choice) - 1][0]
            print("Invalid number.")

    return _Prompt(ask)


def input_dialog(
    *, title: str, text: str, default: str = "", password: bool = False
) -> _Prompt:
    def ask():
        if password:
            if not sys.stdin.isatty():
                raise ValueError("Hidden key input requires a terminal")
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                try:
                    return getpass.getpass(f"{text}: ")
                except getpass.GetPassWarning:
                    raise ValueError("Cannot hide key input in this terminal") from None
        suffix = f" [{default}]" if default else ""
        return input(f"{text}{suffix}: ").strip() or default

    return _Prompt(ask)


def message_dialog(*, title: str, text: str) -> _Prompt:
    return _Prompt(lambda: print(f"{title}: {text}"))


def yes_no_dialog(*, title: str, text: str) -> _Prompt:
    return _Prompt(lambda: input(f"{title} {text} [y/N]: ").strip().lower() == "y")


def manage_auth(environment: str, workspace: Path | None = None) -> int:
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError(
            "Auth selector needs a terminal; use auth list/add/use/model instead"
        )
    while True:
        data = read_profiles(environment)
        rows = [
            (
                name,
                f"{'* ' if name == data['active'] else ''}{name} | {profile['provider']} | {profile['model']}",
            )
            for name, profile in sorted(data["profiles"].items())
        ]
        rows.append(("+", "Add provider"))
        selected = radiolist_dialog(
            title=f"LiteraryGiant | {environment}",
            text="Model profiles",
            values=rows,
            ok_text="Select",
            cancel_text="Close",
        ).run()
        if selected is None:
            return 0
        try:
            if selected == "+":
                _add(environment, workspace)
                continue
            profile = data["profiles"][selected]
            action = radiolist_dialog(
                title=selected,
                text=f"{profile['provider']} | {profile['protocol']}\n{profile['base_url']}",
                values=[
                    ("use", "Activate"),
                    ("model", "Change model"),
                    ("login", "Sign in with ChatGPT")
                    if profile["auth_mode"] == "chatgpt"
                    else ("key", "Replace API key"),
                ],
            ).run()
            if action == "use":
                save_profile(environment, selected)
                return 0
            if action == "model":
                model = input_dialog(
                    title=selected, text="Model ID", default=profile["model"]
                ).run()
                if model is not None:
                    save_profile(environment, selected, model=model.strip())
            if action == "key":
                key = input_dialog(
                    title=selected, text="New API key", password=True
                ).run()
                if key is not None:
                    save_profile(environment, selected, key, replace=True)
            if action == "login":
                from .subscription_auth import login_subscription

                code = login_subscription(
                    environment,
                    selected,
                    workspace or Path.cwd(),
                    model=profile["model"],
                )
                if code:
                    return code
        except ValueError as exc:
            message_dialog(title="Configuration error", text=str(exc)).run()


def _add(environment: str, workspace: Path | None = None) -> None:
    provider = radiolist_dialog(
        title="Provider",
        text="Connection type",
        values=[
            ("codex", "ChatGPT subscription (official Codex login)"),
            *[(name, preset.name) for name, preset in PROVIDERS.items()],
        ],
    ).run()
    if provider is None:
        return
    if provider == "codex":
        from .subscription_auth import login_subscription

        name = input_dialog(
            title="ChatGPT subscription", text="Profile name", default="gpt"
        ).run()
        if name is not None:
            code = login_subscription(environment, name, workspace or Path.cwd())
            if code:
                message_dialog(
                    title="Login not completed",
                    text=f"Codex exited with status {code}; the previous active profile is unchanged.",
                ).run()
        return
    preset = PROVIDERS[provider]
    name = input_dialog(title=preset.name, text="Profile name", default=provider).run()
    if name is None:
        return
    checked_name(name)
    model = input_dialog(
        title=preset.name,
        text="Model ID available to your account",
        default=preset.default_model,
    ).run()
    if model is None:
        return
    endpoint = input_dialog(
        title=preset.name, text="Base URL", default=preset.base_url
    ).run()
    if endpoint is None:
        return
    profile_settings(provider, model.strip(), endpoint.strip())
    exists = name in read_profiles(environment)["profiles"]
    if exists and not yes_no_dialog(title="Replace profile?", text=name).run():
        return
    key = input_dialog(title=preset.name, text="API key", password=True).run()
    if key is not None:
        save_profile(
            environment,
            name,
            key,
            replace=exists,
            provider=provider,
            model=model.strip(),
            base_url=endpoint.strip(),
        )
