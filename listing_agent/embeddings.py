from __future__ import annotations

import io
import math
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

    vectors = [vector for vector, _ in samples]
    targets = [1.0 if label == "like" else 0.0 for _, label in samples]
    like_count = sum(targets)
    dislike_count = len(targets) - like_count
    sample_weights = [dislike_count if target else like_count for target in targets]
    mean_weight = sum(sample_weights) / len(sample_weights)
    sample_weights = [weight / mean_weight for weight in sample_weights]
    weights = [0.0] * len(vectors[0])
    bias = 0.0
    # This small model does not need a heavyweight tensor dependency at fit time.
    for _ in range(300):
        gradients = [0.0] * len(weights)
        bias_gradient = 0.0
        for vector, target, sample_weight in zip(vectors, targets, sample_weights):
            score = sum(weight * value for weight, value in zip(weights, vector)) + bias
            probability = 1.0 / (1.0 + math.exp(-max(-60.0, min(60.0, score))))
            error = (probability - target) * sample_weight
            for index, value in enumerate(vector):
                gradients[index] += error * value
            bias_gradient += error
        scale = 1.0 / len(vectors)
        for index in range(len(weights)):
            weights[index] -= 0.05 * (gradients[index] * scale + 0.01 * weights[index])
        bias -= 0.05 * bias_gradient * scale
    return PreferenceClassifier(weights, bias)
