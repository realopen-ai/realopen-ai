"""Ollama's public catalog has HTML discovery; installation uses its runtime API."""

import re
from html.parser import HTMLParser
from urllib.parse import quote, unquote

from .http import metadata
from .types import CatalogModel, ModelManagerError, Variant
from .validation import model_name


class ModelLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.href = None
        self.text: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self.href = dict(attrs).get("href")
            self.text = []

    def handle_data(self, data):
        if self.href:
            self.text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self.href:
            self.links.append(
                (unquote(self.href), " ".join(" ".join(self.text).split()))
            )
            self.href = None


def _model_link(ident):
    return (
        "https://ollama.com/"
        + ("library/" if "/" not in ident else "")
        + quote(ident, safe="/:")
    )


def _size(text):
    match = re.search(r"\b(\d+(?:\.\d+)?)\s*(KB|MB|GB|TB)\b", text, re.I)
    if match:
        return int(
            float(match[1])
            * 1000 ** ({"KB": 1, "MB": 2, "GB": 3, "TB": 4}[match[2].upper()])
        )
    return None


class OllamaCatalog:
    id = "ollama"
    label = "Ollama Hub"

    async def search(self, query):
        html = await metadata(
            "https://ollama.com/search", params={"q": query}, as_json=False
        )
        parser = ModelLinks()
        parser.feed(html)
        results = {}
        for href, text in parser.links:
            if not href.startswith("/library/"):
                continue
            ident = href.removeprefix("/library/")
            try:
                model_name(ident)
            except ModelManagerError:
                continue
            results[ident] = CatalogModel(
                catalog=self.id,
                id=ident,
                title=ident,
                url=_model_link(ident),
                description=text[:350],
            )
        return list(results.values())[:30]

    async def inspect(self, model, revision=None):
        model = model_name(model)
        base, _, requested_tag = model.partition(":")
        html = await metadata(_model_link(base) + "/tags", as_json=False)
        parser = ModelLinks()
        parser.feed(html)
        variants = {}
        prefix = "/" + ("library/" if "/" not in base else "") + base + ":"
        for href, text in parser.links:
            if not href.startswith(prefix):
                continue
            ident = href[len(prefix) :]
            try:
                model_name(base + ":" + ident)
            except ModelManagerError:
                continue
            if ident in variants and variants[ident].size is not None:
                continue  # Desktop duplicate anchors omit mobile-row size metadata.
            quant = re.search(r"\b(?:[QFBI]\d+(?:_[A-Z0-9]+)*|FP\d+)\b", text, re.I)
            variants[ident] = Variant(
                id=ident,
                label=ident,
                format="ollama",
                size=_size(text),
                quantization=quant[0] if quant else None,
            )
        if requested_tag and requested_tag not in variants:
            raise ModelManagerError("That model tag is not listed in Ollama Hub.")
        if not variants:
            raise ModelManagerError(
                "No installable tags found. The catalog may be temporarily unavailable."
            )
        return CatalogModel(
            catalog=self.id,
            id=base,
            title=base,
            url=_model_link(base),
            variants=list(variants.values())[:500],
        )

    async def download(self, model, variant):
        # Ollama owns pull/resume/storage for its registry models.
        yield {"stage": "preparing", "message": "Preparing runtime-managed download"}
