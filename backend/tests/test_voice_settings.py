import json

import pytest

from app.services import voice_settings


@pytest.fixture()
def settings_file(tmp_path, monkeypatch):
    path = tmp_path / "voice-settings.json"
    monkeypatch.setattr(voice_settings, "_path", lambda: path)
    return path


def test_defaults_are_spoken_voice_defaults(settings_file):
    assert voice_settings.get()["speed"] == 1.0
    assert voice_settings.get()["persona"] == "friendly"
    assert voice_settings.get()["voice"]


def test_settings_round_trip_and_custom_persona(settings_file):
    custom = {"id": "custom-coach", "name": "Coach", "prompt": "Ask crisp questions."}
    result = voice_settings.update({
        "voice": "custom:sample", "speed": 1.5,
        "custom_personas": [custom], "persona": "custom-coach",
    })
    assert result["voice"] == "custom:sample"
    assert result["speed"] == 1.5
    assert voice_settings.persona_prompt() == "Ask crisp questions."
    assert json.loads(settings_file.read_text())["persona"] == "custom-coach"


@pytest.mark.parametrize("speed", [0.1, 0.75, 3])
def test_rejects_unsupported_speed(settings_file, speed):
    with pytest.raises(ValueError, match="Speed"):
        voice_settings.update({"speed": speed})


def test_rejects_unknown_persona(settings_file):
    with pytest.raises(ValueError, match="Unknown persona"):
        voice_settings.update({"persona": "missing"})
