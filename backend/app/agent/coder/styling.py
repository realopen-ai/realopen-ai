"""Local frontend styling adapters shared by blank and application scaffolds."""

import json
from pathlib import Path

ASSETS = Path(__file__).with_name("starters") / "vite" / "frontend"
THEMES = Path(__file__).with_name("styles")
DEFAULT_STYLING = "tailwind"
STYLING = {
    "tailwind": {
        "request_names": ["Tailwind", "TailwindCSS", "Tailwind CSS"],
        "theme": "tailwind.css",
        "dependencies": {},
        "dev_dependencies": {"tailwindcss": "^4.3.3", "@tailwindcss/vite": "^4.3.3"},
        "imports": 'import tailwindcss from "@tailwindcss/vite";',
        "plugins": "tailwindcss()",
    },
}


def validate_styling(name):
    if not isinstance(name, str) or name not in STYLING:
        raise ValueError(
            "Choose a supported frontend styling option: " + ", ".join(STYLING)
        )
    return name


def apply_styling(files, name=DEFAULT_STYLING, *, prefix=""):
    """Configure a new Vite project, preserving shared CRUD/UI source and proxy."""
    style = STYLING[validate_styling(name)]
    root = prefix + "frontend/"
    package = json.loads(
        files.get(root + "package.json", (ASSETS / "package.json").read_text())
    )
    for source, target in (
        ("dependencies", "dependencies"),
        ("dev_dependencies", "devDependencies"),
    ):
        for dependency, version in style[source].items():
            previous = package[target].get(dependency)
            if previous and previous != version:
                raise ValueError(f"Styling conflicts with dependency {dependency}")
            package[target][dependency] = version
    files[root + "package.json"] = json.dumps(package, indent=2) + "\n"
    files[root + "src/theme.css"] = (THEMES / style["theme"]).read_text()
    entrypoint = "src/main.tsx" if root + "src/main.tsx" in files else "src/main.ts"
    main = files[root + entrypoint]
    files[root + entrypoint] = 'import "./theme.css";\n' + main
    config = files.get(root + "vite.config.ts", (ASSETS / "vite.config.ts").read_text())
    if (
        "// STYLING_IMPORTS" not in config
        or "...[] /* STYLING_PLUGINS */" not in config
    ):
        raise ValueError("Vite scaffold must preserve its styling composition hooks")
    files[root + "vite.config.ts"] = config.replace(
        "// STYLING_IMPORTS", style["imports"]
    ).replace("...[] /* STYLING_PLUGINS */", style["plugins"])
