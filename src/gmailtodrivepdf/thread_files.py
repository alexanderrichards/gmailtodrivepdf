"""Identify thread exports by metadata and allocate collision-safe subject names."""

import re
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from . import pdf


@dataclass
class ThreadFile:
    id: str
    subject: str
    path: Path | None = None


def subject_stem(subject: str) -> str:
    stem = re.sub(r"\s+", " ", subject or "no subject")
    stem = pdf._INVALID_FILENAME_CHARS.sub("_", stem).strip(" ._") or "no subject"
    stem = stem[:120].rstrip(" ._")
    if stem.split(".")[0].upper() in {
        "CON", "PRN", "AUX", "NUL",
        *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10)),
    }:
        stem = "_" + stem
    return stem


class ThreadFiles:
    def __init__(self, directory: Path, remote_subjects: dict[str, str] | None = None) -> None:
        self.directory = directory
        self.entries = {
            id: ThreadFile(id, subject) for id, subject in (remote_subjects or {}).items()
        }
        self.unmanaged: set[str] = set()
        for path in directory.iterdir():
            if not path.is_file() or path.suffix.lower() != ".pdf":
                self.unmanaged.add(path.name.casefold())
                continue
            metadata = pdf.read_thread_metadata(path)
            id = metadata.get(pdf.THREAD_ID_KEY)
            if id is None:
                self.unmanaged.add(path.name.casefold())
                continue
            if id in self.entries and self.entries[id].path is not None:
                raise ValueError(f"Multiple local PDFs claim Gmail thread {id}")
            subject = metadata.get(pdf.THREAD_SUBJECT_KEY)
            if subject is None:
                raise ValueError(f"Thread PDF {path} is missing subject metadata")
            self.entries[id] = ThreadFile(id, subject, path)

    def assign(self, id: str, subject: str) -> dict[str, Path]:
        self.entries.setdefault(id, ThreadFile(id, subject)).subject = subject
        stems = {id: subject_stem(entry.subject) for id, entry in self.entries.items()}
        suffix: set[str] = set()
        while True:
            names = {
                id: f"{stem} [{id}].pdf" if id in suffix else f"{stem}.pdf"
                for id, stem in stems.items()
            }
            groups: dict[str, list[str]] = {}
            for id, name in names.items():
                groups.setdefault(name.casefold(), []).append(id)
            collisions = {
                id for name, ids in groups.items()
                if len(ids) > 1 or name in self.unmanaged for id in ids
            }
            if not collisions:
                break
            if collisions <= suffix:
                raise ValueError("Cannot allocate thread filenames without overwriting existing files")
            suffix.update(collisions)
        destinations = {id: self.directory / name for id, name in names.items()}
        moves = [
            (entry, entry.path, destinations[id])
            for id, entry in self.entries.items()
            if entry.path is not None and entry.path != destinations[id]
        ]
        # Stage every move first so swapping titles cannot overwrite another thread.
        staged: list[tuple[ThreadFile, Path, Path, Path]] = []
        try:
            for entry, source, target in moves:
                temporary = self.directory / f".thread-rename-{uuid4().hex}.part"
                source.rename(temporary)
                staged.append((entry, source, temporary, target))
            for entry, source, temporary, target in staged:
                if target.exists():
                    raise FileExistsError(f"Refusing to overwrite {target}")
                temporary.rename(target)
                entry.path = target
        except OSError:
            for entry, source, temporary, target in staged:
                if not temporary.exists():
                    target.rename(temporary)
            for entry, source, temporary, target in staged:
                temporary.rename(source)
                entry.path = source
            raise
        return destinations

    def saved(self, id: str, path: Path) -> None:
        self.entries[id].path = path
