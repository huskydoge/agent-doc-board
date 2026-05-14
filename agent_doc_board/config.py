"""Project configuration loading for Agent Doc Board."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import tomllib


@dataclass(frozen=True)
class CategoryRule:
    """Pattern rules used to place markdown files into one board category."""

    id: str
    title: str
    patterns: tuple[str, ...]
    description: str = ""


@dataclass(frozen=True)
class TodoSeed:
    """Initial TODO entry configured for a project."""

    priority: str
    title: str
    status: str = "todo"
    links: tuple[str, ...] = ()


@dataclass(frozen=True)
class DataRef:
    """Non-markdown data artifact that should be visible from the board."""

    path: str
    title: str
    description: str = ""


@dataclass(frozen=True)
class ProjectConfig:
    """Loaded project configuration with defaults."""

    include: tuple[str, ...] = ("docs/**/*.md",)
    exclude: tuple[str, ...] = ()
    bibliography: tuple[str, ...] = ()
    categories: tuple[CategoryRule, ...] = field(default_factory=tuple)
    todos: tuple[TodoSeed, ...] = field(default_factory=tuple)
    data_refs: tuple[DataRef, ...] = field(default_factory=tuple)


def load_config(root: Path) -> ProjectConfig:
    """Load `.agent-docs/config.toml` or return a safe default config."""
    config_path = root / ".agent-docs" / "config.toml"
    if not config_path.exists():
        return ProjectConfig(categories=default_categories())

    with config_path.open("rb") as handle:
        raw = tomllib.load(handle)

    categories = tuple(
        CategoryRule(
            id=str(item["id"]),
            title=str(item.get("title", item["id"])),
            patterns=tuple(str(pattern) for pattern in item.get("patterns", ())),
            description=str(item.get("description", "")),
        )
        for item in raw.get("categories", ())
    )
    todos = tuple(
        TodoSeed(
            priority=str(item.get("priority", "P2")),
            title=str(item["title"]),
            status=str(item.get("status", "todo")),
            links=tuple(str(link) for link in item.get("links", ())),
        )
        for item in raw.get("todos", ())
    )
    data_refs = tuple(
        DataRef(
            path=str(item["path"]),
            title=str(item.get("title", item["path"])),
            description=str(item.get("description", "")),
        )
        for item in raw.get("data_refs", ())
    )

    return ProjectConfig(
        include=tuple(str(pattern) for pattern in raw.get("include", ("docs/**/*.md",))),
        exclude=tuple(str(pattern) for pattern in raw.get("exclude", ())),
        bibliography=tuple(str(path) for path in raw.get("bibliography", raw.get("bibtex", ()))),
        categories=categories or default_categories(),
        todos=todos,
        data_refs=data_refs,
    )


def default_categories() -> tuple[CategoryRule, ...]:
    """Return fallback categories for projects without explicit config."""
    return (
        CategoryRule("paper-writing", "Paper Writing", ("docs/*paper*.md", "docs/*definition*.md")),
        CategoryRule("experiments-results", "Experiment Results", ("docs/*results*.md", "docs/*cost*.md")),
        CategoryRule("experiment-plans", "Experiment Plans", ("docs/plans/*.md",)),
        CategoryRule("eval-and-metrics", "Eval and Metrics", ("docs/evaluate/**/*.md", "docs/*metrics*.md")),
        CategoryRule("systems-and-training", "Systems and Training", ("docs/*training*.md", "docs/*backprop*.md")),
        CategoryRule("external-settings", "External Settings", ("docs/*dataset*.md", "docs/*external*.md", "docs/*setting*.md")),
    )
