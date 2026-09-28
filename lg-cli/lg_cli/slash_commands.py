"""Discoverable command catalog and private, cross-book usage preferences."""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from dataclasses import dataclass
from pathlib import Path

CATEGORIES = {
    'conversation': '对话', 'read': '阅读与编辑', 'write': '创作',
    'review': '审查与记忆', 'project': '书籍与项目', 'settings': '设置与工具',
}
ROOT_GROUPS = {
    'conversation': {'resume', 'rename', 'new', 'clear', 'help', 'exit', 'chat'},
    'read': {'novel', 'outlines', 'browse', 'chapter', 'scene', 'version', 'characters', 'events', 'storylines', 'timeline', 'export'},
    'write': {'outline', 'write', 'world', 'character', 'plot', 'ref', 'edit'},
    'review': {'check', 'review', 'analysis', 'bible', 'memory', 'foreshadowing', 'run'},
    'project': {'home', 'open', 'focus', 'shelf', 'newbook', 'project', 'init'},
}


@dataclass(frozen=True)
class CommandArgument:
    name: str
    option: str = ''
    choices: tuple[str, ...] = ()
    multiple: bool = False


@dataclass(frozen=True)
class SlashCommand:
    text: str
    description: str
    category: str = 'settings'
    arguments: tuple[CommandArgument, ...] = ()
    children: bool = False


def category_for(text: str) -> str:
    root = text.lstrip('/').split()[0]
    return next((group for group, names in ROOT_GROUPS.items() if root in names), 'settings')


def uses_model(text: str) -> bool:
    """Presentation only: dispatch still uses the explicit CLI parser/handlers."""
    import shlex
    try:
        parts = shlex.split(text.lstrip('/'))
    except ValueError:
        return False
    return bool(parts) and (parts[0] in {'chat', 'outline', 'write', 'world', 'character',
        'plot', 'ref', 'edit', 'check', 'review', 'code'} or parts[:2] in
        [['run', 'resume'], ['scene', 'plan'], ['scene', 'draft'], ['bible', 'curate'],
         ['analysis', 'chapter'], ['analysis', 'volume']])


def command_catalog(parser: argparse.ArgumentParser) -> list[SlashCommand]:
    result: dict[str, SlashCommand] = {}

    def visit(current, prefix='', depth=0):
        if depth >= 4:
            return
        for action in current._actions:
            if not isinstance(action, argparse._SubParsersAction):
                continue
            descriptions = {choice.dest: choice.help for choice in action._choices_actions}
            for name, child in action.choices.items():
                path = '/' + f'{prefix} {name}'.strip()
                args = []
                children = False
                for item in child._actions:
                    if isinstance(item, argparse._SubParsersAction):
                        children = True
                    elif (item.option_strings and item.required) or (not item.option_strings and item.nargs not in ('?', '*')):
                        args.append(CommandArgument(item.dest, item.option_strings[-1] if item.option_strings else '',
                            tuple(map(str, item.choices or ())), item.nargs in ('+', '*')))
                result[path] = SlashCommand(path, descriptions.get(name) or child.description or '',
                    category_for(path), tuple(args), children)
                visit(child, path[1:], depth + 1)

    visit(parser)
    aliases = {
        '/home': '书籍工作台', '/profile': '个人信息与偏好', '/permissions': '操作与创作权限', '/preferences': '个人信息与偏好',
        '/browse': '浏览当前界面', '/rename': '修改对话名称',
        '/help': '命令帮助', '/clear': '清空当前画面', '/exit': '退出', '/model': '选择模型',
        '/resume': '找回历史对话', '/new': '开始新对话', '/open': '打开书籍目录',
        '/focus': '切换书籍', '/shelf': '选择书架目录', '/novel': '阅读与编辑章节',
        '/outlines': '阅读与编辑已有大纲', '/newbook': '创建新书', '/auth': '模型与账户设置',
    }
    for text, description in aliases.items():
        result[text] = SlashCommand(text, description, category_for(text),
            (CommandArgument('directory'),) if text in {'/open', '/shelf'} else ())
    return list(result.values())


def matching_commands(text: str, commands: list[SlashCommand]) -> list[SlashCommand]:
    if not text.startswith('/') or '\n' in text:
        return []
    query = text[1:].strip().casefold()
    if not query:
        return list(commands)
    exact = next((c for c in commands if c.text[1:].casefold() == query), None)
    if exact and not text.endswith(' '):
        return [exact]
    if exact and exact.children:
        return [c for c in commands if c.text.startswith(exact.text + ' ')]
    # Arguments already typed must remain ordinary input, never select an unrelated command.
    if any(text.startswith(c.text + ' ') and not c.children for c in commands):
        return []
    words = query.split()
    matches = [c for c in commands if all(word in (c.text + ' ' + c.description + ' ' + CATEGORIES[c.category]).casefold() for word in words)]
    return sorted(matches, key=lambda c: (not c.text[1:].casefold().startswith(query), c.text.count(' ')))


class CommandUsage:
    def __init__(self, path: Path | None = None):
        self.path = path or Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'literarygiant' / 'command-usage.json'
        self.counts: dict[str, int] = {}
        self.warning = ''
        try:
            data = json.loads(self.path.read_text(encoding='utf-8'))
            self.counts = {k: v for k, v in data.items() if isinstance(k, str) and type(v) is int and v >= 0}
        except (OSError, ValueError, AttributeError):
            pass

    def record(self, text: str, commands: list[SlashCommand]) -> None:
        matches = [c for c in commands if text == c.text or text.startswith(c.text + ' ')]
        if not matches:
            return
        command = max(matches, key=lambda c: len(c.text)).text
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
            with os.fdopen(fd, 'r+', encoding='utf-8') as stream:
                fcntl.flock(stream, fcntl.LOCK_EX)
                try:
                    data = json.load(stream)
                    self.counts = {k: v for k, v in data.items() if isinstance(k, str) and type(v) is int and v >= 0}
                except (ValueError, AttributeError):
                    self.counts = {}
                self.counts[command] = self.counts.get(command, 0) + 1
                stream.seek(0)
                json.dump(self.counts, stream, ensure_ascii=False)
                stream.truncate()
                stream.flush()
                os.fsync(stream.fileno())
            self.path.chmod(0o600)
        except OSError:
            self.warning = '常用命令次数未能保存，本次操作仍可继续。'

    def favorites(self, commands: list[SlashCommand]) -> list[SlashCommand]:
        defaults = ['/resume', '/novel', '/outlines']
        eligible = [c for c in commands if not c.children]
        return sorted(eligible, key=lambda c: (-self.counts.get(c.text, 0),
            defaults.index(c.text) if c.text in defaults else len(defaults), c.text))[:3]
