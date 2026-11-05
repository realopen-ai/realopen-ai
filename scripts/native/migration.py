"""Explicit migration: keep original Docker volumes and a portable DB dump."""

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import os
import re
import uuid

from .config import atomic_json, dotenv

DESTINATIONS = {
    ("postgres", "/var/lib/postgresql/data"): "postgres/docker",
    ("redis", "/data"): "valkey/docker",
    ("backend", "/app/uploads"): "uploads",
    ("backend", "/app/state"): "state",
    ("backend", "/var/cache/apt"): "cache/apt",
    ("backend", "/var/lib/apt/lists"): "cache/apt-lists",
    ("nginx", "/var/cache/nginx"): "cache/nginx",
    ("grafana", "/var/lib/grafana"): "monitoring/grafana",
    ("victoria-metrics", "/victoria-metrics-data"): "monitoring/victoria-metrics",
}


def docker(*args, **kwargs):
    if args and args[0] in {"ps", "inspect", "volume"}:
        kwargs.setdefault("timeout", 15)
    return subprocess.run(["docker", *args], check=True, **kwargs)


def containers(root):
    ids = docker(
        "ps",
        "-aq",
        "--filter",
        f"label=com.docker.compose.project.working_dir={root}",
        capture_output=True,
        text=True,
    ).stdout.split()
    return (
        json.loads(docker("inspect", *ids, capture_output=True, text=True).stdout)
        if ids
        else []
    )


def legacy_mounts(root):
    result = []
    project_containers = containers(root)
    for container in project_containers:
        service = container["Config"]["Labels"]["com.docker.compose.service"]
        for mount in container["Mounts"]:
            destination = DESTINATIONS.get((service, mount["Destination"]))
            if destination and mount["Type"] == "volume":
                result.append(
                    {
                        "volume": mount["Name"],
                        "destination": destination,
                        "container": container["Id"],
                        "image": container["Image"],
                        "service": service,
                    }
                )
    # Include orphaned Compose volumes after `compose down`; never silently
    # initialize a new database merely because the old container was removed.
    project = (
        os.environ.get("COMPOSE_PROJECT_NAME")
        or dotenv(root / ".env").get("COMPOSE_PROJECT_NAME")
        or root.name
    )
    names = docker(
        "volume",
        "ls",
        "-q",
        "--filter",
        f"label=com.docker.compose.project={project}",
        capture_output=True,
        text=True,
    ).stdout.split()
    by_name = {
        "postgres-data": ("postgres", "postgres/docker"),
        "upload-data": ("backend", "uploads"),
        "module-state": ("backend", "state"),
        "apt-cache": ("backend", "cache/apt"),
        "apt-lists": ("backend", "cache/apt-lists"),
        "nginx-cache": ("nginx", "cache/nginx"),
        "grafana-data": ("grafana", "monitoring/grafana"),
        "vm-data": ("victoria-metrics", "monitoring/victoria-metrics"),
    }
    if names:
        volumes = json.loads(
            docker("volume", "inspect", *names, capture_output=True, text=True).stdout
        )
        for volume in volumes:
            logical = (volume.get("Labels") or {}).get("com.docker.compose.volume")
            if logical in by_name and not any(
                x["volume"] == volume["Name"] for x in result
            ):
                service, destination = by_name[logical]
                result.append(
                    {
                        "volume": volume["Name"],
                        "destination": destination,
                        "container": None,
                        "image": "pgvector/pgvector:pg17",
                        "service": service,
                    }
                )
    # Only adopt legacy workspaces known to THIS project's database, not other
    # installations whose containers use the same application label.
    known_workspaces = set()
    postgres = next(
        (
            x
            for x in project_containers
            if x["Config"]["Labels"]["com.docker.compose.service"] == "postgres"
            and x["State"]["Running"]
        ),
        None,
    )
    if postgres:
        try:
            known_workspaces = set(
                docker(
                    "exec",
                    postgres["Id"],
                    "sh",
                    "-c",
                    'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "select volume_name from sandboxes"',
                    capture_output=True,
                    text=True,
                ).stdout.splitlines()
            )
        except subprocess.SubprocessError:
            pass  # An older schema may not have sandboxes yet.
    ids = docker(
        "ps",
        "-aq",
        "--filter",
        "label=ai.realopen.sandbox",
        capture_output=True,
        text=True,
    ).stdout.split()
    if ids:
        for container in json.loads(
            docker("inspect", *ids, capture_output=True, text=True).stdout
        ):
            for mount in container["Mounts"]:
                if (
                    mount["Type"] == "volume"
                    and mount["Destination"] == "/workspace"
                    and mount["Name"] in known_workspaces
                ):
                    result.append(
                        {
                            "volume": mount["Name"],
                            "destination": "sandboxes/" + mount["Name"],
                            "container": container["Id"],
                            "image": container["Image"],
                            "service": "sandbox",
                        }
                    )
    return list({item["volume"]: item for item in result}.values())


def check_legacy(root):
    try:
        mounts = legacy_mounts(Path(root).resolve())
    except (OSError, subprocess.SubprocessError):
        return  # Docker is optional for native runs.
    if mounts:
        marker = Path(root) / "data/migrations/volumes.json"
        copied = json.loads(marker.read_text()) if marker.exists() else {}
        if any(item["volume"] not in copied for item in mounts):
            raise RuntimeError(
                "Legacy Docker volumes found. Run python3 scripts/migrate-storage.py before switching storage or starting native mode. Original volumes will be retained."
            )


def extract_volume_archive(archive, directory, *, workspace=False):
    """Preserve container symlinks without ever extracting files through them.

    Sandbox virtualenvs legitimately point outside /workspace inside the
    container. Create those links only after all file and hard-link writes.
    Other volumes retain tarfile's strict data filter, including link targets.
    """
    directory = Path(directory)
    with tarfile.open(archive) as source:
        if not workspace:
            source.extractall(directory, filter="data")
            return
        symlinks, hardlinks = [], []
        for member in source:
            if member.issym():
                symlinks.append(member)
            elif member.islnk():
                hardlinks.append(member)
            else:
                source.extract(member, directory, filter="data")
        for member in hardlinks:
            safe = tarfile.data_filter(member, directory)
            target = directory / safe.linkname
            if target.is_symlink() or not target.is_file():
                raise tarfile.FilterError(
                    "Hard link must reference an extracted regular file"
                )
            destination = directory / safe.name
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.link(target, destination)
        for member in symlinks:
            # Validate the destination independently of the container-only
            # target. This also rejects paths through a previously made link
            # that would escape the staging directory.
            destination_member = member.replace(linkname="")
            destination_member.type = tarfile.REGTYPE
            safe = tarfile.data_filter(destination_member, directory)
            destination = directory / safe.name
            if os.path.lexists(destination):
                raise tarfile.FilterError("Symlink conflicts with an extracted path")
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(member.linkname, destination)


def migrate_storage(root):
    root = Path(root).resolve()
    data = root / "data"
    data.mkdir(exist_ok=True)
    entries = legacy_mounts(root)
    marker = data / "migrations/volumes.json"
    copied = json.loads(marker.read_text()) if marker.exists() else {}
    pending = [x for x in entries if x["volume"] not in copied]
    managed = containers(root)
    if not pending and not any(
        x["Config"]["Labels"]["com.docker.compose.service"] == "postgres"
        for x in managed
    ):
        print("No Docker database or unmigrated legacy volumes found; nothing changed.")
        return
    for item in pending:
        target = data / item["destination"]
        if target.exists() and any(target.iterdir()):
            raise RuntimeError(
                f"Refusing to overwrite nonempty destination {target}. Resolve it before migrating."
            )
    # Quiesce app writers first. Export with the running source PostgreSQL, then
    # stop all involved containers before copying any physical data directory.
    for container in managed:
        if (
            container["Config"]["Labels"]["com.docker.compose.service"] != "postgres"
            and container["State"]["Running"]
        ):
            docker("stop", "--time", "30", container["Id"], capture_output=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = data / "migrations" / timestamp
    backup.mkdir(parents=True, exist_ok=False)
    postgres = next(
        (
            x
            for x in managed
            if x["Config"]["Labels"]["com.docker.compose.service"] == "postgres"
        ),
        None,
    )
    temporary_postgres = None
    if postgres is None:
        orphan = next((x for x in entries if x["service"] == "postgres"), None)
        if orphan:
            users = docker(
                "ps",
                "-q",
                "--filter",
                f"volume={orphan['volume']}",
                capture_output=True,
                text=True,
            ).stdout.strip()
            if users:
                raise RuntimeError(
                    "The source database volume is used by another running container; stop it before migration"
                )
            temporary_postgres = "realopen-migration-" + uuid.uuid4().hex[:12]
            docker(
                "run",
                "-d",
                "--name",
                temporary_postgres,
                "--network",
                "none",
                "--mount",
                f"type=volume,src={orphan['volume']},dst=/var/lib/postgresql/data",
                "-e",
                "POSTGRES_USER=realopen",
                "-e",
                "POSTGRES_DB=realopen",
                orphan["image"],
                capture_output=True,
            )
            postgres = {
                "Id": temporary_postgres,
                "State": {"Running": False},
                "Image": orphan["image"],
            }
    if postgres:
        dump = backup / "realopen.dump"
        try:
            if not postgres["State"]["Running"]:
                docker("start", postgres["Id"], capture_output=True)
                docker(
                    "exec",
                    postgres["Id"],
                    "sh",
                    "-c",
                    'until pg_isready -U "$POSTGRES_USER"; do sleep 1; done',
                    capture_output=True,
                    timeout=60,
                )
            with dump.open("wb") as output:
                docker(
                    "exec",
                    postgres["Id"],
                    "sh",
                    "-c",
                    'exec pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" --format=custom --no-owner --no-acl',
                    stdout=output,
                )
            # Stopped/idle sandbox containers may already have been removed.
            # The database, rather than global labels, identifies their owner.
            table = docker(
                "exec",
                postgres["Id"],
                "sh",
                "-c",
                'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT to_regclass(\'public.sandboxes\')"',
                capture_output=True,
                text=True,
            ).stdout.strip()
            names = (
                docker(
                    "exec",
                    postgres["Id"],
                    "sh",
                    "-c",
                    'exec psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Atc "SELECT volume_name FROM sandboxes"',
                    capture_output=True,
                    text=True,
                ).stdout.splitlines()
                if table
                else []
            )
            for name in names:
                if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", name):
                    raise RuntimeError("Invalid sandbox volume name in source database")
                if name in copied or any(x["volume"] == name for x in pending):
                    continue
                exists = docker(
                    "volume",
                    "ls",
                    "-q",
                    "--filter",
                    f"name=^{name}$",
                    capture_output=True,
                    text=True,
                ).stdout.splitlines()
                if name not in exists:
                    continue
                target = data / "sandboxes" / name
                if target.exists() and any(target.iterdir()):
                    raise RuntimeError(
                        f"Refusing to overwrite nonempty destination {target}"
                    )
                active = docker(
                    "ps",
                    "-q",
                    "--filter",
                    f"volume={name}",
                    capture_output=True,
                    text=True,
                ).stdout.split()
                for container_id in active:
                    docker("stop", "--time", "30", container_id, capture_output=True)
                pending.append(
                    {
                        "volume": name,
                        "destination": "sandboxes/" + name,
                        "container": None,
                        "image": postgres["Image"],
                        "service": "sandbox",
                    }
                )
        finally:
            if temporary_postgres:
                docker("stop", "--time", "60", temporary_postgres, capture_output=True)
                docker("rm", temporary_postgres, capture_output=True)
        with dump.open("rb") as source:
            digest = hashlib.file_digest(source, "sha256").hexdigest()
        atomic_json(
            data / "migrations/database.json",
            {"dump": str(dump.relative_to(data)), "sha256": digest},
        )
    for container_id in {x["container"] for x in pending if x["container"]}:
        docker("stop", "--time", "60", container_id, capture_output=True)
    if postgres and not temporary_postgres:
        docker("stop", "--time", "60", postgres["Id"], capture_output=True)
    for item in pending:
        target = data / item["destination"]
        archive = backup / (item["volume"] + ".tar")
        with archive.open("wb") as output:
            helper = postgres["Image"] if postgres else "pgvector/pgvector:pg17"
            docker(
                "run",
                "--rm",
                "--network",
                "none",
                "--read-only",
                "--entrypoint",
                "tar",
                "--mount",
                f"type=volume,src={item['volume']},dst=/source,readonly",
                helper,
                "-C",
                "/source",
                "-cf",
                "-",
                ".",
                stdout=output,
            )
        staging = target.with_name(target.name + ".migrating")
        if staging.exists():
            raise RuntimeError(
                f"Incomplete prior staging directory found: {staging}. Inspect it before retrying."
            )
        staging.mkdir(parents=True)
        try:
            extract_volume_archive(
                archive, staging, workspace=item["service"] == "sandbox"
            )
            if target.exists():
                target.rmdir()
            staging.rename(target)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        copied[item["volume"]] = {
            "destination": item["destination"],
            "archive": str(archive.relative_to(data)),
        }
        atomic_json(marker, copied)
        print(f"Copied {item['volume']} to {target}; original volume retained.")
    print(
        "Storage migration/export complete. Containers remain stopped. Start Docker normally, or run start-native migrate to restore the database natively."
    )


def restore_native(cfg, services):
    marker = cfg.data / "migrations/database.json"
    if not marker.exists():
        raise RuntimeError(
            "No Docker database export. Run scripts/migrate-storage.py first, before recreating old containers."
        )
    metadata = json.loads(marker.read_text())
    dump = (cfg.data / metadata["dump"]).resolve()
    if not dump.is_relative_to(cfg.data.resolve()):
        raise RuntimeError("Invalid database export path")
    with dump.open("rb") as source:
        digest = hashlib.file_digest(source, "sha256").hexdigest()
    if digest != metadata["sha256"]:
        raise RuntimeError("Database export checksum mismatch")
    if not cfg.managed["DATABASE_URL"]:
        raise RuntimeError(
            "Restore only targets the private managed native database; external databases are never overwritten"
        )
    services.database()
    tables = services.psql("SELECT tablename FROM pg_tables WHERE schemaname='public'")
    if tables:
        restored = cfg.runtime / "migration.json"
        if (
            restored.exists()
            and json.loads(restored.read_text()).get("sha256") == metadata["sha256"]
        ):
            print(
                "This export was already restored; current native data left unchanged."
            )
            return
        raise RuntimeError(
            "Target native database is not empty; refusing to restore over it"
        )
    subprocess.run(
        [
            cfg.postgres_bin("pg_restore"),
            "--single-transaction",
            "--exit-on-error",
            "--no-owner",
            "--no-privileges",
            "--dbname",
            cfg.pg_env()["PGDATABASE"],
            str(dump),
        ],
        env=cfg.pg_env(),
        check=True,
    )
    atomic_json(
        cfg.runtime / "migration.json",
        {"restored_from": metadata["dump"], "sha256": metadata["sha256"]},
    )
    print("Native database restored. Original Docker volumes and export retained.")
