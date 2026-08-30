from __future__ import annotations

import io
import os
from functools import lru_cache

import httpx
from PIL import Image


@lru_cache(maxsize=1)
def _model():
    from transformers import CLIPModel, CLIPProcessor

    name = os.environ.get("CLIP_MODEL", "openai/clip-vit-base-patch32")
    return CLIPProcessor.from_pretrained(name), CLIPModel.from_pretrained(name)


def image_embedding(url: str) -> list[float]:
    response = httpx.get(url, timeout=30, follow_redirects=True)
    response.raise_for_status()
    image = Image.open(io.BytesIO(response.content)).convert("RGB")
    processor, model = _model()
    import torch

    inputs = processor(images=image, return_tensors="pt")
    with torch.no_grad():
        vector = model.get_image_features(**inputs)[0]
    vector = vector / vector.norm()
    return vector.tolist()


def cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))
