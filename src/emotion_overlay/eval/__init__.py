"""Offline evaluation: run labelled clips through the models and report accuracy and latency.

python -m emotion_overlay.eval download ravdess
python -m emotion_overlay.eval record
python -m emotion_overlay.eval run ravdess        (or: run mine)
python -m emotion_overlay.eval report ravdess     (re-analyse cached predictions)
"""
