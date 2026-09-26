"""Compact keyboard selectors and hidden credential input."""

from __future__ import annotations

import asyncio
import getpass
import os
import sys
import warnings
from collections.abc import Callable
from contextlib import redirect_stderr, redirect_stdout
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.application.current import get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.output import ColorDepth

from .credentials import checked_name, read_profiles, save_profile
from .providers import PROVIDERS, profile_settings
from .terminal_input import LiteraryInput, choice_rows, input_style

_backend = ContextVar("lg_auth_ui", default=None)


async def run_auth(session, action, *, refresh=None):
    if not callable(getattr(session, "close_dialog", None)):
        raise TypeError(
            "Terminal UI was updated during this session; exit and restart literary before opening /auth"
        )
    loop = asyncio.get_running_loop()

    async def present(spec):
        if refresh:
            refresh()
        return await session.ask(**spec)

    def ask(**spec):
        return asyncio.run_coroutine_threadsafe(present(spec), loop).result()

    class Output:
        def write(self, text):
            loop.call_soon_threadsafe(session.append, text)
            return len(text)

        def flush(self):
            pass

        def isatty(self):
            return True

    def command(argv, *, env, cwd):
        return asyncio.run_coroutine_threadsafe(
            session.run_command(argv, env=env, cwd=cwd), loop
        ).result()

    def worker():
        token = _backend.set((ask, command))
        try:
            with redirect_stdout(Output()), redirect_stderr(Output()):
                return action()
        finally:
            _backend.reset(token)

    try:
        return await asyncio.to_thread(worker)
    finally:
        session.close_dialog()
        if refresh:
            refresh()


class _Prompt:
    def __init__(self, action: Callable[[], Any]) -> None:
        self.action = action

    def run(self) -> Any:
        return self.action()


def choice_application(*, title: str, text: str, values: list, input=None, output=None):
    selected = 0
    keys = KeyBindings()

    @keys.add("up")
    def previous(event):
        nonlocal selected
        selected = (selected - 1) % len(values) if values else 0

    @keys.add("down")
    def following(event):
        nonlocal selected
        selected = (selected + 1) % len(values) if values else 0

    @keys.add("enter")
    def accept(event):
        event.app.exit(result=values[selected][0] if values else None)

    @keys.add("escape")
    @keys.add("c-c")
    @keys.add("c-d")
    def cancel(event):
        event.app.exit(result=None)

    def rows():
        width = max(1, get_app().output.get_size().columns - 1)
        return choice_rows([label for _, label in values], selected, width)

    choices = Window(
        FormattedTextControl(
            rows,
            get_cursor_position=lambda: Point(
                0, selected + (os.environ.get("TERM") != "dumb")
            ),
        ),
        height=Dimension(min=1, max=12),
        dont_extend_height=True,
        always_hide_cursor=True,
    )
    return Application(
        layout=Layout(
            HSplit(
                [
                    Window(
                        FormattedTextControl(title + ("\n" + text if text else "")),
                        dont_extend_height=True,
                    ),
                    choices,
                ]
            ),
            focused_element=choices,
        ),
        key_bindings=keys,
        style=input_style(),
        color_depth=ColorDepth.DEPTH_24_BIT,
        erase_when_done=True,
        full_screen=True,
        input=input,
        output=output,
    )


def radiolist_dialog(*, title: str, text: str, values: list, **_kwargs: Any) -> _Prompt:
    if _backend.get():
        return _Prompt(
            lambda: _backend.get()[0](
                kind="choice", title=title, text=text, values=values
            )
        )
    return _Prompt(
        lambda: choice_application(title=title, text=text, values=values).run()
    )


def input_dialog(
    *, title: str, text: str, default: str = "", password: bool = False
) -> _Prompt:
    if _backend.get():
        return _Prompt(
            lambda: _backend.get()[0](
                kind="input", title=title, text=text, default=default, password=password
            )
        )

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
    return _Prompt(
        lambda: (
            radiolist_dialog(
                title=title, text=text, values=[(False, "Cancel"), (True, "Confirm")]
            ).run()
            is True
        )
    )


def manage_auth(environment: str, workspace: Path | None = None) -> int:
    if _backend.get():
        return _manage_auth(environment, workspace)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise ValueError(
            "Auth selector needs a terminal; use auth list/add/use/model instead"
        )
    from .config import load_config
    from .dashboard import render_status_bar

    config = load_config(workspace, environment=environment)
    history = config.workspace / ".literarygiant" / "history"
    history.parent.mkdir(parents=True, exist_ok=True)
    session = LiteraryInput(
        history,
        status_bar=lambda width: render_status_bar(
            config, width, compact=session.app.output.get_size().rows < 20
        ),
    )

    def refresh():
        nonlocal config
        config = load_config(workspace, environment=environment)
        session.model, session.provider = config.model_label, config.provider

    async def handle(_):
        await run_auth(
            session, lambda: _manage_auth(environment, workspace), refresh=refresh
        )
        return False

    return session.run(
        handle, config.model_label, config.provider, initial_command="auth"
    )


def _login_subscription(*args, **kwargs):
    from .subscription_auth import login_subscription

    if _backend.get():
        kwargs["run_process"] = _backend.get()[1]
    return login_subscription(*args, **kwargs)


def _manage_auth(environment: str, workspace: Path | None = None) -> int:
    if not _backend.get() and (not sys.stdin.isatty() or not sys.stdout.isatty()):
        raise ValueError(
            "Auth selector needs a terminal; use auth list/add/use/model instead"
        )
    while True:
        data = read_profiles(environment)
        rows = [
            (
                name,
                profile_label(name, profile, data),
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
                code = _login_subscription(
                    environment,
                    selected,
                    workspace or Path.cwd(),
                    model=profile["model"],
                )
                if code:
                    return code
        except ValueError as exc:
            message_dialog(title="Configuration error", text=str(exc)).run()


def profile_label(name: str, profile: dict, data: dict) -> str:
    provider = profile['provider']
    preset = PROVIDERS.get(provider)
    label = f"{preset.name if preset else provider} | {profile['model']}"
    duplicates = sum(
        item['provider'] == provider and item['model'] == profile['model']
        for item in data['profiles'].values()
    )
    if duplicates > 1:
        label += f" [{name}]"
    return ("* " if name == data['active'] else "") + label


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
        name = input_dialog(
            title="ChatGPT subscription", text="Profile name", default="gpt"
        ).run()
        if name is not None:
            code = _login_subscription(environment, name, workspace or Path.cwd())
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
