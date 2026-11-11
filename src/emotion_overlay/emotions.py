"""Canonical emotion labels shared by the models, the state machine and the UI.

These match the labels of the default text model. Other models (e.g. the Phase 3
audio model) must map their labels onto this set explicitly.
"""

EMOTIONS: tuple[str, ...] = ("anger", "disgust", "fear", "joy", "neutral", "sadness", "surprise")
NEUTRAL = "neutral"

Scores = dict[str, float]
