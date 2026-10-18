"""Feature composition, independent of model prompting and sandbox publication.

Features contribute files, dependencies and lifecycle installers. API dependencies
and mutation observers share one runtime context; frontend plugins wrap one
transport. Adding Auth/WS should not fork a CRUD+Auth+WS template.
"""

import json
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from app.agent.coder.scaffolding.contracts import Contribution, Feature
from app.agent.coder.scaffolding.crud import render_crud, asset
from app.agent.coder.starters import FRONTENDS, frontend_assets
from app.agent.coder.styling import apply_styling


FEATURES = {"crud": Feature("crud", render_crud)}
RESERVED_OUTPUTS = {
    "backend/app.py",
    "backend/services.py",
    "frontend/src/main.ts",
    "frontend/src/main.tsx",
    "frontend/src/extensions.ts",
    "frontend/package.json",
    "frontend/vite.config.ts",
    "frontend/src/theme.css",
    ".realopen-app.json",
    ".realopen-project.json",
    "pyproject.toml",
}


@dataclass
class BuildPlan:
    files: dict = field(default_factory=dict)
    features: dict = field(default_factory=dict)
    backend: dict = field(
        default_factory=lambda: {"dependencies": [], "dev_dependencies": []}
    )
    frontend: dict = field(
        default_factory=lambda: {"dependencies": {}, "dev_dependencies": {}}
    )
    installers: list = field(default_factory=list)
    routers: list = field(default_factory=list)
    plugins: list = field(default_factory=list)
    resources: list = field(default_factory=list)

    def add(self, feature, contribution):
        for path, content in contribution.files.items():
            parsed = PurePosixPath(path)
            if parsed.is_absolute() or ".." in parsed.parts or "\\" in path:
                raise ValueError(f"Feature has an unsafe output path: {path}")
            if path in self.files:
                raise ValueError(f"Features conflict on output file: {path}")
            if path in RESERVED_OUTPUTS or str(parsed) != path:
                raise ValueError(
                    f"Feature must use composition hooks, not replace {path}"
                )
            self.files[path] = content
        for key, values in (
            ("dependencies", contribution.backend_dependencies),
            ("dev_dependencies", contribution.backend_dev_dependencies),
        ):
            self.backend[key] = list(dict.fromkeys([*self.backend[key], *values]))
        for key, values in (
            ("dependencies", contribution.frontend_dependencies),
            ("dev_dependencies", contribution.frontend_dev_dependencies),
        ):
            for name, version in values.items():
                previous = self.frontend[key].get(name)
                if previous and previous != version:
                    raise ValueError(f"Features require conflicting versions of {name}")
                self.frontend[key][name] = version
        self.installers.extend(contribution.backend_installers)
        self.routers.extend(contribution.backend_routers)
        self.plugins.extend(contribution.frontend_plugins)
        self.features[feature.name] = feature.version

    def manifest(self):
        return {
            "backend": self.backend,
            "metadata": {
                "generator": "crud",
                "version": 1,
                "features": self.features,
                "resources": self.resources,
            },
        }


def build(spec, *, features=None, registry=None):
    """Resolve prerequisites once, reject cycles/conflicts before any workspace write."""
    registry = FEATURES if registry is None else registry
    features = spec.features if features is None else features
    plan = BuildPlan()
    plan.resources = [entity.name for entity in spec.entities]
    visiting = set()

    def visit(name):
        if name in visiting:
            raise ValueError(f"Feature dependency cycle involving {name}")
        if name in plan.features:
            return
        if name not in registry:
            raise ValueError(f"Unsupported scaffold feature: {name}")
        feature = registry[name]
        if feature.name != name:
            raise ValueError("Feature registry key must match its name")
        visiting.add(name)
        for requirement in feature.requires:
            visit(requirement)
        plan.add(feature, feature.render(spec))
        visiting.remove(name)

    for name in features:
        visit(name)
    for selected, adapters in (
        (spec.backend, BACKEND_SHELLS),
        (spec.frontend, FRONTEND_SHELLS),
    ):
        if selected == "none":
            continue
        if selected not in adapters:
            raise ValueError(f"No composition shell for stack {selected}")
        adapters[selected](spec, plan)
    plan.files[".realopen-app.json"] = (
        json.dumps(
            {
                "schema_version": 1,
                "generator_version": 1,
                "features": plan.features,
                "spec": spec.model_dump(),
            },
            indent=2,
            ensure_ascii=False,
        )
        + "\n"
    )
    return plan


def fastapi_shell(spec, plan):
    plan.files["backend/services.py"] = asset("backend/services.py.tpl")
    imports, installers, routers = [], [], []
    for i, dotted in enumerate(plan.installers):
        module, function = dotted.rsplit(".", 1)
        imports.append(f"from {module} import {function} as install_{i}")
        installers.append(f"    install_{i}(app, services)")
    for i, dotted in enumerate(plan.routers):
        module, function = dotted.rsplit(".", 1)
        imports.append(f"from {module} import {function} as router_{i}")
        routers.append(f"    app.include_router(router_{i}(services))")
    plan.files["backend/app.py"] = (
        asset("backend/app.py.tpl")
        .replace("# FEATURE_IMPORTS", "\n".join(imports))
        .replace("    # FEATURE_INSTALLERS", "\n".join(installers))
        .replace("    # FEATURE_ROUTERS", "\n".join(routers))
        .replace("APP_TITLE", repr(spec.name))
    )


def vite_shell(spec, plan):
    for path, content in frontend_assets(spec.frontend).items():
        plan.files.setdefault(path, content)
    package = json.loads(plan.files["frontend/package.json"])
    for key, values in plan.frontend.items():
        target = "devDependencies" if key == "dev_dependencies" else key
        for name, version in values.items():
            previous = package[target].get(name)
            if previous and previous != version:
                raise ValueError(
                    f"Feature conflicts with frontend starter dependency: {name}"
                )
            package[target][name] = version
    plan.files["frontend/package.json"] = json.dumps(package, indent=2) + "\n"
    plan.files["frontend/src/extensions.ts"] = asset("frontend/extensions.ts.tpl")
    imports = [
        f"import {{ install as plugin{i} }} from {json.dumps(module)};"
        for i, module in enumerate(plan.plugins)
    ]
    plugins = ", ".join(f"plugin{i}" for i in range(len(plan.plugins)))
    entrypoint = FRONTENDS[spec.frontend]["entrypoint"]
    template = (
        "frontend/react/main.tsx.tpl"
        if FRONTENDS[spec.frontend]["framework"] == "react"
        else "frontend/main.ts.tpl"
    )
    plan.files["frontend/" + entrypoint] = (
        asset(template)
        .replace("// FEATURE_IMPORTS", "\n".join(imports))
        .replace("/* FEATURE_PLUGINS */", plugins)
    )
    apply_styling(plan.files, spec.styling)


BACKEND_SHELLS = {"fastapi": fastapi_shell}
FRONTEND_SHELLS = {"vite": vite_shell, "react-vite": vite_shell}
