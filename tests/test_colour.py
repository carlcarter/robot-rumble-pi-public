"""Unit tests for the nearest-centroid colour classifier."""

from colour import ColourClassifier


def test_default_centroids_classify_their_own_centre():
    c = ColourClassifier()
    assert c.classify(40, 60, 220) == "blue"
    assert c.classify(220, 40, 40) == "red"
    assert c.classify(40, 200, 60) == "green"
    assert c.classify(230, 210, 40) == "yellow"
    assert c.classify(20, 20, 20) == "black"
    assert c.classify(230, 230, 230) == "white"


def test_small_noise_still_classifies_blue():
    c = ColourClassifier()
    assert c.classify(45, 70, 215) == "blue"


def test_grey_falls_outside_threshold():
    c = ColourClassifier()
    # Mid-grey is roughly equidistant from black and white — both are further than
    # the default 90-unit threshold allows.
    assert c.classify(128, 128, 128) == "unknown"
