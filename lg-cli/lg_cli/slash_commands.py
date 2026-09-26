"""Slash-command suggestions derived from the real CLI parser."""

from __future__ import annotations

import argparse
from dataclasses import dataclass


@dataclass(frozen=True)
class SlashCommand:
    text: str
    description: str


def command_catalog(parser: argparse.ArgumentParser) -> list[SlashCommand]:
    result = [
        SlashCommand("/help", "Show commands"),
        SlashCommand("/clear", "Clear the screen, keep saved history"),
        SlashCommand("/exit", "Close LiteraryGiant"),
        SlashCommand("/model", "Choose a model profile"),
        SlashCommand("/resume", "Restore a conversation in this project"),
        SlashCommand("/new", "Start a new conversation in this project"),
        SlashCommand("/open", "Open a book project directory"),
        SlashCommand("/focus", "Choose the active book project"),
        SlashCommand("/shelf", "Select the bookshelf root directory"),
    ]

    def visit(current, prefix="", depth=0):
        if depth >= 3:
            return
        for action in current._actions:
            if isinstance(action, argparse._SubParsersAction):
                descriptions = {
                    choice.dest: choice.help for choice in action._choices_actions
                }
                for name, child in action.choices.items():
                    path = f"{prefix} {name}".strip()
                    result.append(
                        SlashCommand(
                            "/" + path,
                            descriptions.get(name) or child.description or "",
                        )
                    )
                    visit(child, path, depth + 1)

    visit(parser)
    return sorted(result, key=lambda command: command.text)


def matching_commands(text: str, commands: list[SlashCommand]) -> list[SlashCommand]:
    if not text.startswith("/") or "\n" in text:
        return []
    query = text[1:]
    if " " not in query:
        return [
            command
            for command in commands
            if " " not in command.text and command.text[1:].startswith(query)
        ]
    return [
        command
        for command in commands
        if command.text.startswith(text) and command.text != text.rstrip()
    ]
