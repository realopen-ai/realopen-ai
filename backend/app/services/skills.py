"""Filesystem-backed local skills with safe import and compact routing metadata."""

from __future__ import annotations

import io
import re
import shutil
import zipfile
from pathlib import Path, PurePosixPath

import yaml

SKILL_FILE = "SKILL.md"
VALID_ROLES = {"general", "coder", "voice"}
MAX_IMPORT_BYTES = 10 * 1024 * 1024
MAX_FILES = 200
MAX_INSTRUCTIONS = 200_000
SKILLS_DIR = (
    Path("/app/data/skills")
    if Path("/app/data").exists()
    else Path(__file__).resolve().parents[3] / "data" / "skills"
)


def skills_root() -> Path:
    root = SKILLS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")[:64]
    if not slug:
        raise ValueError("Skill name must contain letters or numbers")
    return slug


def _frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        raise ValueError("SKILL.md must start with YAML frontmatter")
    marker = text.find("\n---\n", 4)
    if marker < 0:
        raise ValueError("SKILL.md frontmatter is not closed")
    meta = yaml.safe_load(text[4:marker]) or {}
    if not isinstance(meta, dict):
        raise ValueError("Skill frontmatter must be a mapping")
    return meta, text[marker + 5 :]


def _normalize(meta: dict, fallback: str) -> dict:
    name = str(meta.get("name") or fallback).strip()
    description = str(meta.get("description") or "").strip()
    if not name or not description:
        raise ValueError("Skill name and description are required")
    roles = meta.get("roles") or []
    if isinstance(roles, str):
        roles = [roles]
    roles = sorted({str(role).lower() for role in roles})
    invalid = set(roles) - VALID_ROLES
    if invalid:
        raise ValueError(f"Unknown skill roles: {', '.join(sorted(invalid))}")
    return {
        "name": name[:100],
        "description": description[:500],
        "roles": roles,
        "enabled": bool(meta.get("enabled", True)),
    }


def _render(meta: dict, body: str) -> str:
    header = yaml.safe_dump(meta, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{header}\n---\n\n{body.strip()}\n"


def _directory(slug: str) -> Path:
    safe = slugify(slug)
    if safe != slug:
        raise ValueError("Invalid skill identifier")
    return skills_root() / safe


def _record(directory: Path, include_content: bool = False) -> dict:
    text = (directory / SKILL_FILE).read_text(encoding="utf-8")
    meta, body = _frontmatter(text)
    normalized = _normalize(meta, directory.name)
    files = [
        str(path.relative_to(directory))
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]
    result = {"id": directory.name, **normalized, "files": files}
    if include_content:
        result.update({"content": body.strip(), "markdown": text})
    return result


def list_skills(role: str | None = None, enabled_only: bool = False) -> list[dict]:
    records = []
    for directory in sorted(skills_root().iterdir()):
        if not directory.is_dir() or not (directory / SKILL_FILE).is_file():
            continue
        try:
            item = _record(directory)
        except (OSError, ValueError, yaml.YAMLError):
            continue
        if enabled_only and not item["enabled"]:
            continue
        if role and item["roles"] and role not in item["roles"]:
            continue
        records.append(item)
    return records


def get_skill(slug: str) -> dict:
    directory = _directory(slug)
    if not (directory / SKILL_FILE).is_file():
        raise FileNotFoundError(slug)
    return _record(directory, include_content=True)


def save_skill(
    *,
    name: str,
    description: str,
    roles: list[str],
    enabled: bool,
    content: str,
    skill_id: str | None = None,
) -> dict:
    if len(content.encode("utf-8")) > MAX_INSTRUCTIONS:
        raise ValueError("Skill instructions exceed 200 KB")
    meta = _normalize(
        {"name": name, "description": description, "roles": roles, "enabled": enabled},
        name,
    )
    slug = skill_id or slugify(name)
    directory = _directory(slug)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / SKILL_FILE).write_text(_render(meta, content), encoding="utf-8")
    return get_skill(slug)


def delete_skill(slug: str) -> None:
    directory = _directory(slug)
    if not directory.exists():
        raise FileNotFoundError(slug)
    shutil.rmtree(directory)


def import_zip(data: bytes) -> dict:
    if len(data) > MAX_IMPORT_BYTES:
        raise ValueError("Skill archive exceeds 10 MB")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        infos = [item for item in archive.infolist() if not item.is_dir()]
        if len(infos) > MAX_FILES:
            raise ValueError("Skill archive contains too many files")
        if sum(item.file_size for item in infos) > MAX_IMPORT_BYTES:
            raise ValueError("Extracted skill exceeds 10 MB")
        skill_entries = [
            item for item in infos if PurePosixPath(item.filename).name == SKILL_FILE
        ]
        if len(skill_entries) != 1:
            raise ValueError("Skill archive must contain exactly one SKILL.md")
        prefix = PurePosixPath(skill_entries[0].filename).parent
        text = archive.read(skill_entries[0]).decode("utf-8")
        if len(text.encode("utf-8")) > MAX_INSTRUCTIONS:
            raise ValueError("Skill instructions exceed 200 KB")
        meta, _body = _frontmatter(text)
        normalized = _normalize(meta, prefix.name or "skill")
        slug = slugify(normalized["name"])
        destination = _directory(slug)
        if destination.exists():
            raise ValueError(f"Skill '{slug}' already exists")
        destination.mkdir(parents=True)
        try:
            for info in infos:
                if (info.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Skill archive cannot contain symbolic links")
                path = PurePosixPath(info.filename)
                if path.is_absolute() or ".." in path.parts:
                    raise ValueError("Skill archive contains an unsafe path")
                try:
                    relative = path.relative_to(prefix)
                except ValueError:
                    continue
                if not relative.parts:
                    continue
                target = destination.joinpath(*relative.parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(info))
            return get_skill(slug)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise


def import_files(files: list[tuple[str, bytes]]) -> dict:
    if not files or len(files) > MAX_FILES:
        raise ValueError("Select one skill folder containing SKILL.md")
    if sum(len(data) for _name, data in files) > MAX_IMPORT_BYTES:
        raise ValueError("Skill folder exceeds 10 MB")
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in files:
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("Skill folder contains an unsafe path")
            archive.writestr(str(path), data)
    return import_zip(buffer.getvalue())


def routing_catalog(role: str) -> str:
    items = list_skills(role=role, enabled_only=True)
    if not items:
        return ""
    lines = ["Available skills (load one only when relevant):"]
    lines.extend(f"- {item['id']}: {item['description']}" for item in items)
    lines.append("Use load_skill with the skill id before following its instructions.")
    return "\n".join(lines)


def read_resource(slug: str, resource: str) -> str:
    relative = PurePosixPath(resource)
    if relative.is_absolute() or ".." in relative.parts or resource == SKILL_FILE:
        raise ValueError("Invalid skill resource path")
    target = _directory(slug).joinpath(*relative.parts)
    if not target.is_file() or target.is_symlink():
        raise FileNotFoundError(resource)
    data = target.read_bytes()
    if len(data) > MAX_INSTRUCTIONS:
        raise ValueError("Skill resource is too large")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Skill resource is not a text file") from exc


def load_for_agent(slug: str, role: str, resource: str | None = None) -> str:
    item = get_skill(slug)
    if not item["enabled"] or (item["roles"] and role not in item["roles"]):
        raise ValueError("Skill is disabled or unavailable for this agent role")
    print(f"Loading skill {slug} for role {role} with resource={resource}")
    if resource:
        return read_resource(slug, resource)
    if len(item["markdown"]) > MAX_INSTRUCTIONS:
        raise ValueError("Skill instructions are too large")
    resources = [path for path in item["files"] if path != SKILL_FILE]
    inventory = "\n".join(f"- {path}" for path in resources[:100]) or "(none)"
    return (
        f"Skill: {item['name']}\n\n{item['markdown']}\n\n"
        f"Bundled resources (load with load_skill resource=path):\n{inventory}"
    )
