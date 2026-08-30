from __future__ import annotations

import io
import os
from functools import lru_cache
from dataclasses import dataclass

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


@dataclass
class PreferenceClassifier:
    """Small linear classifier over normalized image embeddings."""

    weights: list[float]
    bias: float
    constant_label: str | None = None

    def predict(self, vector: list[float]) -> tuple[str, float]:
        if self.constant_label:
            return self.constant_label, 1.0
        import math

        score = sum(weight * value for weight, value in zip(self.weights, vector)) + self.bias
        probability = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, score))))
        return ("like" if probability >= 0.5 else "dislike"), probability


def fit_preference_classifier(samples: list[tuple[list[float], str]]) -> PreferenceClassifier | None:
    """Fit logistic regression from all currently active labeled examples.

    Re-fitting this tiny model from persisted embeddings is intentional: it makes
    every newly labeled example immediately affect future predictions without
    storing model weights or sending taste prompts to an LLM.
    """
    if not samples:
        return None
    labels = {label for _, label in samples if label in {"like", "dislike"}}
    if not labels:
        return None
    if len(labels) == 1:
        return PreferenceClassifier([], 0.0, next(iter(labels)))

    import torch

    vectors = torch.tensor([vector for vector, _ in samples], dtype=torch.float32)
    targets = torch.tensor([1.0 if label == "like" else 0.0 for _, label in samples], dtype=torch.float32)
    counts = torch.bincount(targets.to(torch.int64), minlength=2).float()
    sample_weights = torch.where(targets == 1, counts[0], counts[1])
    sample_weights = sample_weights / sample_weights.mean()
    weights = torch.zeros(vectors.shape[1], requires_grad=True)
    bias = torch.zeros(1, requires_grad=True)
    optimizer = torch.optim.Adam([weights, bias], lr=0.05)
    # A small L2 penalty keeps a few feedback examples from overfitting CLIP noise.
    for _ in range(300):
        optimizer.zero_grad()
        logits = vectors @ weights + bias
        loss = torch.nn.functional.binary_cross_entropy_with_logits(logits, targets, weight=sample_weights)
        loss = loss + 0.01 * weights.square().mean()
        loss.backward()
        optimizer.step()
    return PreferenceClassifier(weights.detach().tolist(), float(bias.detach().item()))
