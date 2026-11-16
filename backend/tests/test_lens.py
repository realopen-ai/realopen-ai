"""Regression checks for hardware selection, image inputs and search fusion."""

from io import BytesIO
from uuid import UUID

from PIL import Image

from app.services.lens.config import get_lens_config
from app.services.lens.encoders import image_mime, image_rgb, model_version
from app.services.lens.search import rank
from app.services.model_manager.lens_models import model_spec


def test_lens_model_precision_and_versions_follow_hardware_profiles():
    medium = get_lens_config("apple_medium")
    large = get_lens_config("apple_large")
    assert medium.siglip2 == "int8"
    assert large.siglip2 == "fp32"
    assert medium.e5 == large.e5 == "int8"
    assert medium.threads == large.threads == 2
    assert model_version("siglip2", medium.siglip2) != model_version(
        "siglip2", large.siglip2
    )
    for variant in model_spec("siglip2").variants:
        assert len(variant.files) == 3
        assert all(file.sha256 and len(file.sha256) == 64 for file in variant.files)


def test_lens_uses_image_content_and_flattens_transparency():
    with Image.new("RGBA", (10, 10), (0, 0, 0, 0)) as transparent, BytesIO() as buffer:
        transparent.save(buffer, "PNG")
        data = buffer.getvalue()
    assert image_mime(data) == "image/png"
    with image_rgb(data) as result:
        assert result.getpixel((0, 0)) == (255, 255, 255)
    assert image_mime(b'<svg xmlns="http://www.w3.org/2000/svg"/>') is None


def test_lens_exact_and_semantic_fallbacks_survive_missing_visual_index():
    exact, semantic, weak, visual = (UUID(int=i) for i in range(4))
    assert rank([exact], {}, {}, 0) == [exact]
    assert rank([exact], {}, {semantic: 0.9, weak: 0.81}, 0.7) == [exact, semantic]
    assert rank([exact], {visual: 0.6, semantic: 0.55}, {semantic: 0.94}, 0.7) == [
        exact,
        semantic,
        visual,
    ]
