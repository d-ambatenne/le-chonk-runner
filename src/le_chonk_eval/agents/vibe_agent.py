"""Harbor's Vibe agent, with the two model settings its config.toml leaves out (FRICTIONS row 40).

Vibe bills cached input tokens at the input price unless the model has a `cached_input_price`, and it offers the
model no images unless `supports_images` is set. Both come from VIBE_CACHED_INPUT_PRICE and VIBE_SUPPORTS_IMAGES
when they're set, else from LiteLLM's table, where Harbor takes the other prices.
"""

from __future__ import annotations

import tomllib
from typing import Any

from harbor.agents.installed.vibe import Vibe, _to_toml


class LeChonkVibe(Vibe):
    def _build_config_toml(self) -> str:
        config = tomllib.loads(super()._build_config_toml())
        model = config["models"][0]
        cached = self._cached_input_price()
        if cached is not None:
            model["cached_input_price"] = cached
        images = self._supports_images()
        if images is not None:
            model["supports_images"] = images
        return _to_toml(config)

    def _cached_input_price(self) -> float | None:
        """Dollars per million cached input tokens."""
        explicit = self._get_env("VIBE_CACHED_INPUT_PRICE")
        if explicit:
            try:
                return float(explicit)
            except ValueError:
                raise ValueError(f"VIBE_CACHED_INPUT_PRICE must be dollars per million tokens, not {explicit!r}") from None
        rate = self._litellm_entry().get("cache_read_input_token_cost")
        return round(rate * 1_000_000, 6) if rate else None

    def _supports_images(self) -> bool | None:
        explicit = self._get_env("VIBE_SUPPORTS_IMAGES")
        if explicit:
            return explicit.strip().lower() in ("1", "true", "yes")
        vision = self._litellm_entry().get("supports_vision")
        return None if vision is None else bool(vision)

    def _litellm_entry(self) -> dict[str, Any]:
        try:
            import litellm
        except ImportError:
            return {}
        for key in (self.model_name, self._model_id):
            if key and (entry := litellm.model_cost.get(key)):
                return entry
        return {}
