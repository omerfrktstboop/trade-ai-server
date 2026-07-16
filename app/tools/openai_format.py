"""Registry araçlarını OpenAI-uyumlu function-calling tanımlarına çevir.

DeepSeek, OpenAI chat/completions şemasını konuşur; ``tools`` alanı buradan
beslenir. Sadece ``audience``'ında ``"ai"`` olan araçlar dahil edilir.
"""

from __future__ import annotations

from typing import Any

from app.tools.registry import specs_for_audience


def _clean_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """pydantic'in ürettiği şemadan gürültüyü (title alanları) temizle."""
    schema.pop("title", None)
    for prop in schema.get("properties", {}).values():
        if isinstance(prop, dict):
            prop.pop("title", None)
    return schema


def openai_tool_definitions(audience: str = "ai") -> list[dict[str, Any]]:
    """OpenAI ``tools`` listesi — her araç bir ``function`` girdisi."""
    definitions: list[dict[str, Any]] = []
    for spec in specs_for_audience(audience):
        definitions.append(
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": _clean_schema(spec.params_model.model_json_schema()),
                },
            }
        )
    return definitions
