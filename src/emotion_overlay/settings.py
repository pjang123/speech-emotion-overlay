"""Live-editable settings: the parts of the config the setup page may change at runtime.

Components hold references to their config sections (the state machine reads
`config.state`, the segmenter `config.vad`, ...), so updates are copied into the
existing objects in place; replacing them would leave components on stale copies.

Everything else (models, server address, upload limits, debug recording) needs a
restart or is deliberately not remotely changeable.
"""

from typing import Any

from pydantic import BaseModel, ValidationError

from emotion_overlay.config import AppConfig

# section -> editable fields (None = all fields of that section)
EDITABLE: dict[str, frozenset[str] | None] = {
    "state": None,
    "display": None,
    "vad": None,
    "classifier": frozenset({"window_seconds"}),
    "audio_emotion": frozenset({"enabled", "weight"}),  # model/label_map need a restart
}


class SettingsError(ValueError):
    pass


def editable_settings(config: AppConfig) -> dict[str, dict[str, Any]]:
    result = {}
    for section, fields in EDITABLE.items():
        data = getattr(config, section).model_dump(mode="json")
        result[section] = data if fields is None else {k: data[k] for k in sorted(fields)}
    return result


def validate_settings(
    config: AppConfig, patch: Any, *, strict: bool = True
) -> dict[str, BaseModel]:
    """Validate a partial update without applying it.

    strict=True (API) rejects anything not editable; strict=False (imported setups)
    silently drops it, so a shared setup file can't e.g. turn on debug recording
    or change the server address.
    """
    if not isinstance(patch, dict):
        raise SettingsError("settings must be an object")
    validated: dict[str, BaseModel] = {}
    for section, values in patch.items():
        if section not in EDITABLE:
            if strict:
                raise SettingsError(f"'{section}' settings can't be changed here")
            continue
        if not isinstance(values, dict):
            raise SettingsError(f"'{section}' must be an object")
        current: BaseModel = getattr(config, section)
        allowed = EDITABLE[section] or frozenset(type(current).model_fields)
        unknown = set(values) - allowed
        if unknown and strict:
            raise SettingsError(f"unknown or locked settings: {sorted(unknown)}")
        values = {k: v for k, v in values.items() if k in allowed}
        try:
            validated[section] = type(current).model_validate({**current.model_dump(), **values})
        except ValidationError as exc:
            problems = "; ".join(
                f"{section}.{'.'.join(map(str, e['loc']))}: {e['msg']}" for e in exc.errors()
            )
            raise SettingsError(problems) from exc
    return validated


def commit_settings(config: AppConfig, validated: dict[str, BaseModel]) -> None:
    for section, new in validated.items():
        target = getattr(config, section)
        for name in type(target).model_fields:
            setattr(target, name, getattr(new, name))


def apply_settings(config: AppConfig, patch: Any, *, strict: bool = True) -> None:
    """Validate everything first, then apply: an invalid patch changes nothing."""
    commit_settings(config, validate_settings(config, patch, strict=strict))


def reset_settings(config: AppConfig) -> None:
    """Restore every live-editable setting to its default; locked settings are untouched."""
    apply_settings(config, editable_settings(AppConfig()))
