"""Protein ladder definitions, loaded from ladders.json."""
from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

LADDERS_FILE = Path(__file__).with_name("ladders.json")


@dataclass(frozen=True)
class Ladder:
    id: str
    name: str
    bands_kda: tuple[float, ...]  # high → low MW (top → bottom of the gel)
    reference_kda: tuple[float, ...]
    reference_cue: str | None  # "color_pink" | "intensity" | None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "bands_kda": list(self.bands_kda),
            "reference_kda": list(self.reference_kda),
            "reference_cue": self.reference_cue,
        }


@lru_cache(maxsize=1)
def _load() -> tuple[str, dict[str, Ladder]]:
    data = json.loads(LADDERS_FILE.read_text())
    ladders = {}
    for entry in data["ladders"]:
        bands = tuple(sorted((float(b) for b in entry["bands_kda"]), reverse=True))
        ladders[entry["id"]] = Ladder(
            id=entry["id"],
            name=entry["name"],
            bands_kda=bands,
            reference_kda=tuple(float(b) for b in entry.get("reference_kda", [])),
            reference_cue=entry.get("reference_cue"),
        )
    return data.get("default", next(iter(ladders))), ladders


def all_ladders() -> list[Ladder]:
    return list(_load()[1].values())


def default_ladder_id() -> str:
    return _load()[0]


def get_ladder(ladder_id: str | None) -> Ladder:
    default, ladders = _load()
    key = ladder_id or default
    if key not in ladders:
        raise KeyError(f"Unknown ladder '{key}'. Known: {', '.join(ladders)}")
    return ladders[key]


def format_kda(kda: float) -> str:
    return f"{kda:g}"
