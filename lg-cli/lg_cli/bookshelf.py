"""A bookshelf contains independent book workspaces, never shared story memory."""

import json
import sqlite3
from pathlib import Path

from .init_project import init_workspace
from .project_store import ProjectRegistry, ProjectStore


def remember_location(workspace: Path, environment: str | None = None) -> None:
    from .credentials import environment_dir
    from .output_writer import _atomic_text

    workspace = workspace.resolve()
    shelf = Bookshelf.discover(workspace)
    store = ProjectStore(workspace)
    if not store.initialized and not (shelf and shelf.root == workspace):
        return
    payload = {"schema": "lg.location.v1", "workspace": str(workspace),
               "project_id": store.project_info().project_id if store.initialized else None,
               "shelf": str(shelf.root) if shelf else None}
    directory = environment_dir(environment or "sandbox")
    if directory.is_symlink():
        raise ValueError("Location directory must not be a symlink")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    _atomic_text(directory / "location.json", json.dumps(payload, indent=2) + "\n")


def restore_location(current: Path, environment: str | None = None) -> Path:
    from .credentials import environment_dir

    current = current.resolve()
    # A different local book always wins over a remembered location.
    store = ProjectStore(current)
    agent_checkout = any((current / entry).is_file() for entry in (
        "lg-cli/lg_cli/main.py", "LiteraryAgent/lg-cli/lg_cli/main.py",
    ))
    if store.initialized:
        # Early LG versions initialized empty databases in the source checkout.
        if not agent_checkout or store.list_documents(limit=1):
            return current
    local_shelf = Bookshelf.discover(current)
    directory = environment_dir(environment or "sandbox")
    path = directory / "location.json"
    if directory.is_symlink() or path.is_symlink():
        return current
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("schema") != "lg.location.v1":
            return current
        if local_shelf and str(local_shelf.root) != data.get("shelf"):
            return current
        target = Path(data["workspace"])
        if not target.is_absolute() or target.is_symlink():
            return current
        store = ProjectStore(target)
        if store.initialized and store.project_info().project_id == data.get("project_id"):
            return target.resolve()
        if data.get("shelf"):
            shelf = Bookshelf(Path(data["shelf"]))
            if shelf.marker.is_file() and not ProjectStore(shelf.root).initialized:
                return shelf.root
    except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
        pass
    return current


class Bookshelf:
    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self.marker = self.root / ".literarygiant" / "bookshelf.json"

    def initialize(self):
        if ProjectStore(self.root).initialized:
            raise ValueError(
                "A book cannot also be a bookshelf. Choose its parent directory."
            )
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self.marker.open("x", encoding="utf-8") as stream:
                json.dump({"schema_version": "lg.bookshelf.v1"}, stream)
        except FileExistsError:
            pass

    @classmethod
    def discover(cls, workspace: Path):
        for root in (workspace, workspace.parent):
            shelf = cls(root)
            if shelf.marker.is_file():
                return shelf
        return None

    def books(self):
        result = []
        if not self.root.is_dir():
            return result
        for path in sorted(self.root.iterdir()):
            if not path.is_dir() or path.is_symlink() or path.name.startswith("."):
                continue
            store = ProjectStore(path)
            if store.initialized:
                book = store.project_info()
                result.append(
                    {
                        "name": book.name,
                        "project_id": book.project_id,
                        "workspace": str(path),
                        "workspace_exists": True,
                        "initialized": True,
                    }
                )
        return result

    def check_book(self, path: Path):
        if path.resolve().parent != self.root or path.is_symlink():
            raise ValueError("Choose a book directly inside the current bookshelf.")
        if not ProjectStore(path).initialized:
            raise ValueError("Not an initialized book. Use /newbook first.")

    def create(self, name: str) -> Path:
        name = name.strip()
        if (
            not name
            or name in {".", ".."}
            or name.startswith(".")
            or any(char in name for char in "/\\\0\r\n")
            or len(name) > 100
        ):
            raise ValueError(
                "Book names must be one directory name (1-100 characters), without slashes."
            )
        self.initialize()
        target = self.root / name
        target.mkdir(exist_ok=False)
        init_workspace(target)
        ProjectRegistry().register(ProjectStore(target).rename_project(name))
        return target
