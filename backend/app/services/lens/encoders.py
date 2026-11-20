"""Local model preprocessing based on RealOpen Lens's benchmarked ONNX encoders."""

import asyncio
import multiprocessing
import time
from io import BytesIO

from PIL import Image, ImageOps

from app.services import ocr
from app.services.model_manager.lens_models import MANIFEST, model_paths
from .config import LensConfig

# One ONNX session at a time, including concurrent search and indexing.
encoder_slot = asyncio.Semaphore(1)
ENCODER_TIMEOUT = 60
# macOS SF_DATALESS is absent from Python's stat module on some installations.
SF_DATALESS = 0x40000000
IMAGE_FORMATS = {"JPEG", "MPO", "PNG", "WEBP", "TIFF", "BMP", "GIF"}


class LensEncoderUnavailable(RuntimeError):
    pass


def require_local_models(files):
    if any(getattr(path.stat(), "st_flags", 0) & SF_DATALESS for path in files.values()):
        raise LensEncoderUnavailable(
            "Lens model files are offloaded to cloud storage. In Finder, choose "
            "Keep Downloaded for data/models and wait for downloads to finish."
        )


def _supported(image: Image.Image) -> bool:
    return image.format in IMAGE_FORMATS and image.width * image.height <= 20_000_000


def image_mime(data: bytes) -> str | None:
    if not data or len(data) > ocr.MAX_IMAGE_BYTES:
        return None
    try:
        with Image.open(BytesIO(data)) as image:
            return image.get_format_mimetype() if _supported(image) else None
    except (OSError, ValueError, Image.DecompressionBombError):
        return None


def image_rgb(data: bytes) -> Image.Image:
    if len(data) > ocr.MAX_IMAGE_BYTES:
        raise ValueError("Image exceeds 20 MiB.")
    try:
        with Image.open(BytesIO(data)) as original:
            if not _supported(original):
                raise ValueError(
                    "Image has an unsupported format or exceeds 20 megapixels."
                )
            with ImageOps.exif_transpose(original) as oriented:
                if "A" in oriented.getbands() or "transparency" in oriented.info:
                    with oriented.convert("RGBA") as color, Image.new(
                        "RGBA", color.size, "white"
                    ) as background:
                        background.alpha_composite(color)
                        return background.convert("RGB")
                return oriented.convert("RGB")
    except Image.DecompressionBombError as exc:
        raise ValueError("Image exceeds 20 megapixels.") from exc


def thumbnail(data: bytes) -> bytes:
    with image_rgb(data) as picture, BytesIO() as output:
        picture.thumbnail((480, 480))
        picture.save(output, format="WEBP", quality=80)
        return output.getvalue()


def model_version(ident: str, precision: str) -> str:
    return f"{ident}:{MANIFEST[ident]['revision']}:{precision}:v1"


def _session(ident, precision, graph, threads):
    files = model_paths(ident, precision)
    if not files:
        return None
    require_local_models(files)
    import onnxruntime as ort

    ort.disable_telemetry_events()
    options = ort.SessionOptions()
    options.intra_op_num_threads = threads
    options.inter_op_num_threads = 1
    options.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
    key = MANIFEST[ident]["files"][f"{graph}_{precision}"]["path"]
    return ort.InferenceSession(
        str(files[key]), sess_options=options, providers=["CPUExecutionProvider"]
    )


def _normalize(vectors):
    import numpy as np

    vectors = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(vectors, axis=-1, keepdims=True)
    if not np.isfinite(norms).all() or (norms == 0).any():
        raise ValueError("Encoder returned an invalid embedding.")
    return (vectors / norms).tolist()


def _image_vectors(config: LensConfig, images: list[bytes]):
    import numpy as np

    session = _session("siglip2", config.siglip2, "vision", config.threads)
    if session is None:
        return None
    pixels = []
    for data in images:
        with image_rgb(data) as picture, picture.resize(
            (224, 224), Image.Resampling.BILINEAR
        ) as resized:
            array = np.asarray(resized, dtype=np.float32) * np.float32(1 / 255)
            pixels.append(((array - 0.5) / 0.5).transpose(2, 0, 1))
    vectors = session.run(["pooler_output"], {"pixel_values": np.stack(pixels)})[0]
    return _normalize(vectors)


def _text_vectors(config: LensConfig, ident: str, texts: list[str], passages: bool):
    is_visual = ident == "siglip2"
    precision = config.siglip2 if is_visual else config.e5
    files = model_paths(ident, precision)
    if not files:
        return None
    require_local_models(files)
    session = _session(ident, precision, "text", config.threads)
    if session is None:
        return None
    import numpy as np
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(files["tokenizer.json"]))
    groups = []
    for text in texts:
        if passages:
            ids = tokenizer.encode(text, add_special_tokens=False).ids
            groups.append(
                [
                    "passage: " + tokenizer.decode(ids[i : i + 440])
                    for i in range(0, len(ids), 400)
                ]
            )
        else:
            groups.append([text.lower() if is_visual else "query: " + text])
    tokenizer.enable_truncation(max_length=64 if is_visual else 512)
    tokenizer.enable_padding(
        length=64 if is_visual else None,
        pad_id=0 if is_visual else 1,
        pad_token="<pad>",
    )
    required = {item.name for item in session.get_inputs()}
    flat = [text for group in groups for text in group]
    vectors = []
    for offset in range(0, len(flat), 8):
        encoded = tokenizer.encode_batch(flat[offset : offset + 8])
        ids = np.asarray([item.ids for item in encoded], dtype=np.int64)
        mask = np.asarray([item.attention_mask for item in encoded], dtype=np.int64)
        feeds = {
            "input_ids": ids,
            "attention_mask": mask,
            "token_type_ids": np.zeros_like(ids),
        }
        output = "pooler_output" if is_visual else "last_hidden_state"
        hidden = session.run(
            [output], {k: v for k, v in feeds.items() if k in required}
        )[0]
        vectors.extend(
            _normalize(
                hidden
                if is_visual
                else (hidden * mask[..., None]).sum(1) / mask.sum(1, keepdims=True)
            )
        )
    result = []
    start = 0
    for group in groups:
        result.append(vectors[start : start + len(group)])
        start += len(group)
    return result


def _encoder_worker(connection, function, args):
    try:
        connection.send((True, function(*args)))
    except Exception as exc:
        connection.send((False, str(exc)))
    finally:
        connection.close()


async def _encode(function, *args):
    try:
        await asyncio.wait_for(encoder_slot.acquire(), timeout=10)
    except TimeoutError as exc:
        raise LensEncoderUnavailable("Lens is busy indexing. Please retry shortly.") from exc
    # A native library or cloud-backed file read can stall indefinitely in a
    # thread. A separate process lets cancellation and deadlines stop that work.
    context = multiprocessing.get_context("spawn")
    reader, writer = context.Pipe(duplex=False)
    process = context.Process(target=_encoder_worker, args=(writer, function, args))
    try:
        process.start()
        writer.close()
        deadline = time.monotonic() + ENCODER_TIMEOUT
        while not reader.poll():
            if not process.is_alive():
                raise LensEncoderUnavailable("Lens encoder exited before returning a result.")
            if time.monotonic() >= deadline:
                raise LensEncoderUnavailable(
                    "Lens encoding timed out. Check that model files are downloaded locally."
                )
            await asyncio.sleep(0.05)
        try:
            success, result = await asyncio.to_thread(reader.recv)
        except (EOFError, OSError) as exc:
            raise LensEncoderUnavailable(
                "Lens encoder exited before returning a result."
            ) from exc
        if not success:
            raise LensEncoderUnavailable(result)
        return result
    finally:
        if process.pid is not None:
            if process.is_alive():
                process.kill()
            await asyncio.to_thread(process.join, 5)
            if not process.is_alive():
                process.close()
        reader.close()
        writer.close()
        encoder_slot.release()


async def image_vectors(config: LensConfig, images: list[bytes]):
    return await _encode(_image_vectors, config, images)


async def text_vectors(
    config: LensConfig, ident: str, texts: list[str], *, passages=False
):
    return await _encode(_text_vectors, config, ident, texts, passages)
