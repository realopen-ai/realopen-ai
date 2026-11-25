"""Hardware-profile defaults for Lens's CPU ONNX encoders."""

from dataclasses import dataclass
from typing import Literal

from app.config import _load_raw_profiles_data, settings


@dataclass(frozen=True)
class LensConfig:
    siglip2: Literal["int8", "fp32"] = "int8"
    e5: Literal["int8"] = "int8"
    threads: int = 2

    def __post_init__(self):
        if self.siglip2 not in {"int8", "fp32"} or self.e5 != "int8":
            raise ValueError("Unsupported Lens model precision")
        if not 1 <= self.threads <= 8:
            raise ValueError("Lens threads must be between 1 and 8")


def get_lens_config(profile: str | None = None) -> LensConfig:
    data = _load_raw_profiles_data()
    selected = (data.get("profiles") or {}).get(profile or settings.HARDWARE_PROFILE)
    return LensConfig(
        **{**(data.get("lens") or {}), **((selected or {}).get("lens") or {})}
    )
