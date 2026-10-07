"""
Nearest-centroid RGB classifier.

Used by both FakeRobot (to label the stub colour stream) and SpheroRobot (to label
real colour-sensor samples). Centroids live in schemas/colour_centroids.json so a
venue recalibration never needs a code change.
"""

import json
from pathlib import Path
from typing import Dict, Tuple

_CentroidMap = Dict[str, Tuple[int, int, int]]

DEFAULT_CENTROIDS_PATH = Path(__file__).parent / "schemas" / "colour_centroids.json"


def load_centroids(path: Path = DEFAULT_CENTROIDS_PATH) -> Tuple[_CentroidMap, float]:
    data = json.loads(path.read_text())
    centroids = {name: tuple(rgb) for name, rgb in data["centroids"].items()}
    return centroids, float(data.get("unknown_distance_threshold", 90))


def classify(r: int, g: int, b: int, centroids: _CentroidMap, threshold: float) -> str:
    """
    Return the colour name whose centroid is closest in RGB Euclidean distance.
    Returns "unknown" if the closest centroid is further than `threshold`.
    """
    best_name = "unknown"
    best_dist_sq = threshold * threshold
    for name, (cr, cg, cb) in centroids.items():
        dist_sq = (r - cr) ** 2 + (g - cg) ** 2 + (b - cb) ** 2
        if dist_sq < best_dist_sq:
            best_dist_sq = dist_sq
            best_name = name
    return best_name


class ColourClassifier:
    """Pre-loads centroids once; call classify() per sample."""

    def __init__(self, path: Path = DEFAULT_CENTROIDS_PATH):
        self.centroids, self.threshold = load_centroids(path)

    def classify(self, r: int, g: int, b: int) -> str:
        return classify(r, g, b, self.centroids, self.threshold)
