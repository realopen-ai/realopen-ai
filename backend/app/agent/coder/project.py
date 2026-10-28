"""Small live project snapshots, never inferred from starter defaults or summaries."""

import json
import shlex

from app.services import sandbox_host

MANIFEST_NAMES = {
    "pyproject.toml",
    "package.json",
    "uv.lock",
    "bun.lock",
    "bun.lockb",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
}

# Runs in the workspace, not in the application process. Only manifests and
# installed distribution metadata are inspected; no user code is imported.
PROJECT_SNAPSHOT = r"""
import importlib.metadata, json, re, sys, tomllib
from pathlib import Path
root = Path(sys.argv[1] if len(sys.argv) > 1 else '/workspace')
result = {'manifests': {}, 'installed_python': {}, 'installed_node': {},
          'capabilities': {'browser_tests': Path('/opt/roai-browser/node_modules/playwright/index.mjs').is_file() and Path('/usr/local/bin/roai-browser-test').is_file()}}
python_names = set()
for directory in ['', 'backend', 'frontend']:
    folder = root / directory
    for name in ['pyproject.toml', 'package.json']:
        path = folder / name
        if not path.is_file():
            continue
        key = path.relative_to(root).as_posix()
        try:
            if path.stat().st_size > 65536:
                raise ValueError('manifest too large')
            data = tomllib.loads(path.read_text()) if name.endswith('.toml') else json.loads(path.read_text())
            if name == 'package.json':
                entry = {k: data.get(k, {}) for k in ['scripts', 'dependencies', 'devDependencies']}
                installed = {}
                for package in (entry['dependencies'] | entry['devDependencies']):
                    if not re.fullmatch(r'(?:@[\w.-]+/)?[\w.-]+', package):
                        continue
                    for base in [folder, root]:
                        dependency = base / 'node_modules' / package / 'package.json'
                        if dependency.is_file():
                            installed[package] = json.loads(dependency.read_text()).get('version')
                            break
                result['installed_node'][directory or '.'] = installed
            else:
                project = data.get('project', {})
                groups = data.get('dependency-groups', {})
                entry = {'dependencies': project.get('dependencies', []), 'dependency_groups': groups}
                for spec in project.get('dependencies', []) + [s for group in groups.values() for s in group if isinstance(s, str)]:
                    match = re.match(r'[\w.-]+', spec)
                    if match:
                        python_names.add(re.sub(r'[-_.]+', '-', match[0]).lower())
            result['manifests'][key] = entry
        except Exception as exc:
            result['manifests'][key] = {'error': type(exc).__name__}
paths = [str(p) for p in root.glob('.venv/lib/python*/site-packages')]
for dist in importlib.metadata.distributions(path=paths):
    name = dist.metadata.get('Name', '')
    if re.sub(r'[-_.]+', '-', name).lower() in python_names:
        result['installed_python'][name] = dist.version
print(json.dumps(result))
"""


def configuration_changed(event):
    return any(
        path.rsplit("/", 1)[-1] in MANIFEST_NAMES
        for path in [*event.get("files", []), event.get("file", "")]
    )


async def project_snapshot(sandbox):
    try:
        result = await sandbox_host.call(
            "exec",
            sandbox,
            {"command": "python3 -c " + shlex.quote(PROJECT_SNAPSHOT), "timeout": 15},
            timeout=25,
        )
        if result.get("exit_code") == 0:
            facts = json.loads(result.get("stdout", ""))
            if isinstance(facts, dict) and isinstance(facts.get("manifests"), dict):
                return facts
    except Exception:
        pass
    # Never retain a stale snapshot as though it described current dependencies.
    return None
