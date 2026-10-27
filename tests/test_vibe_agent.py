"""Our Vibe agent: Harbor's, with the cached-input price and image input in Vibe's model config."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

import pytest

from le_chonk_eval.agents.vibe_agent import LeChonkVibe

PRICES = {"VIBE_INPUT_PRICE": "0.68", "VIBE_OUTPUT_PRICE": "2.09"}


def config(tmp_path: Path, model: str = "mistral/mistral-large-4", **env: str) -> dict[str, Any]:
    agent = LeChonkVibe(logs_dir=tmp_path, model_name=model, skills_dir="/skills", extra_env={**PRICES, **env}, max_price=3)
    return tomllib.loads(agent._build_config_toml())


def test_cached_tokens_get_their_own_price_and_the_model_gets_images(tmp_path: Path) -> None:
    written = config(tmp_path)
    model = written["models"][0]
    assert model["input_price"] == 0.68 and model["output_price"] == 2.09
    assert model["cached_input_price"] == pytest.approx(0.068)  # LiteLLM's cache_read_input_token_cost
    assert model["supports_images"] is True
    assert written["active_model"] == "mistral-large-4" and written["skill_paths"] == ["/skills"]  # the rest is Harbor's
    assert written["providers"][0]["api_key_env_var"] == "MISTRAL_API_KEY"


def test_the_environment_overrides_the_table(tmp_path: Path) -> None:
    model = config(tmp_path, VIBE_CACHED_INPUT_PRICE="0.1", VIBE_SUPPORTS_IMAGES="false")["models"][0]
    assert model["cached_input_price"] == 0.1 and model["supports_images"] is False


def test_a_model_the_table_doesnt_know_keeps_harbors_config(tmp_path: Path) -> None:
    model = config(tmp_path, model="mistral/not-a-model")["models"][0]
    assert "cached_input_price" not in model and "supports_images" not in model


def test_a_cached_price_that_isnt_a_number_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="VIBE_CACHED_INPUT_PRICE"):
        config(tmp_path, VIBE_CACHED_INPUT_PRICE="cheap")
