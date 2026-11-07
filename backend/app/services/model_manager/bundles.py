"""Format/variant discovery shared by remote catalogs and local model imports."""

import re

from .types import ModelFile, ModelManagerError, Variant

AUXILIARY_FILES = {
    "config.json",
    "generation_config.json",
    "tokenizer.json",
    "tokenizer_config.json",
    "special_tokens_map.json",
    "tokenizer.model",
    "vocab.json",
    "merges.txt",
    "added_tokens.json",
    "chat_template.jinja",
    "preprocessor_config.json",
    "processor_config.json",
    "model.safetensors.index.json",
}
SPLIT_GGUF = re.compile(r"(.+)-(\d{5})-of-(\d{5})\.gguf$", re.I)


def supported_file(name: str) -> bool:
    return name.lower().endswith((".gguf", ".safetensors")) or name in AUXILIARY_FILES


def discover_variants(
    files: list[ModelFile], config: dict, *, mlx=False, format_hint="auto"
):
    variants = []
    groups = {}
    for item in files:
        if not item.path.lower().endswith(".gguf"):
            continue
        split = SPLIT_GGUF.fullmatch(item.path)
        key = ("split", split[1], split[3]) if split else ("single", item.path, "")
        groups.setdefault(key, []).append(item)
    names = {item.path for item in files}
    for group, members in groups.items():
        split = SPLIT_GGUF.fullmatch(members[0].path) if group[0] == "split" else None
        key = split[1] + "-shards.gguf" if split else members[0].path
        if split:
            pieces = [SPLIT_GGUF.fullmatch(item.path) for item in members]
            count = int(split[3])
            if len(pieces) != count or {int(piece[2]) for piece in pieces} != set(
                range(1, count + 1)
            ):
                continue
            if key in names:
                key = "split:" + split[1] + ":" + split[3]
        quant = re.search(
            r"(?:^|[_.-])((?:[QFBI]\d+(?:_[A-Z0-9]+)*|FP\d+))(?=[_.-]|$)", key, re.I
        )
        variants.append(
            Variant(
                id=key,
                label=key,
                format="gguf",
                size=sum(f.size for f in members),
                quantization=quant[1] if quant else None,
                files=sorted(members, key=lambda f: f.path),
            )
        )
    weights = [
        f for f in files if f.path.endswith(".safetensors") and "/" not in f.path
    ]
    if weights:
        # MLX releases commonly use `quantization`; HF-normalized metadata may
        # instead expose `quantization_config`. An explicit local format choice
        # handles bundles with insufficient metadata without guessing from names.
        quantization = (
            config.get("quantization_config") or config.get("quantization") or {}
        )
        if not isinstance(quantization, dict):
            raise ModelManagerError("The model has invalid quantization metadata.")
        mlx = format_hint == "mlx" or (
            format_hint == "auto"
            and (
                mlx
                or bool(config.get("quantization"))
                or quantization.get("quant_method") == "mlx"
            )
        )
        members = weights + [f for f in files if f.path in AUXILIARY_FILES]
        variants.append(
            Variant(
                id="safetensors",
                label="Safetensors weights" + (" (MLX)" if mlx else ""),
                format="mlx" if mlx else "safetensors",
                quantization=(
                    (str(quantization["bits"]) + "-bit")
                    if quantization.get("bits")
                    else None
                ),
                size=sum(f.size for f in members),
                files=members,
                metadata={
                    "quantized": bool(quantization),
                    "architecture": config.get("model_type"),
                },
            )
        )
    return variants
