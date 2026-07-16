"""Freshness guard for the `suggesting-bindings` skill this overlay ships.

The skill is AGENT-FACING and auto-invoked (a workspace agent consults it to
suggest `[[binding]]` blocks), so a stale catalog silently misleads. It hardcodes
the builtin injector + provider names, which live in the CLI. This tripwire fails
if a builtin is added/renamed without the skill being updated — the same
freshness discipline `tests/cli/test_docs_lint.py` applies to the docs. It does
NOT (and can't cheaply) check CLI-flag / TOML-syntax drift; that obligation is
documented in this overlay's README.
"""
from pathlib import Path

from credproxy_cli.core.model.config import _overlay_source


def _skill_text() -> str:
    """Every markdown file the skill ships, concatenated (SKILL.md + references)."""
    root = Path(_overlay_source("skills/suggesting-bindings", "test"))
    return "\n".join(p.read_text() for p in sorted(root.rglob("*.md")))


def _builtin_names(subdir: str, suffix: str) -> set[str]:
    """The builtin registry names under cli/.../builtin/<subdir> (stems for TOML
    injectors, filenames for provider executables)."""
    builtin = Path(__file__).resolve().parents[3] / "cli" / "credproxy_cli" / "builtin"
    d = builtin / subdir
    if suffix:
        return {p.stem for p in d.glob(f"*{suffix}")}
    # providers are executables (a file) or a dir with a `run` — the name is the entry.
    return {p.name for p in d.iterdir() if p.name != "__pycache__"}


def test_skill_covers_every_builtin_injector():
    injectors = _builtin_names("injectors", ".toml")
    text = _skill_text()
    missing = sorted(name for name in injectors if name not in text)
    assert not missing, (
        f"suggesting-bindings skill omits builtin injector(s): {missing}. "
        f"Update overlay/50-example/skills/suggesting-bindings/ (SKILL.md or "
        f"references/) so agents suggest current injectors."
    )


def test_skill_covers_every_builtin_provider():
    providers = _builtin_names("providers", "")
    text = _skill_text()
    missing = sorted(name for name in providers if name not in text)
    assert not missing, (
        f"suggesting-bindings skill omits builtin provider(s): {missing}. "
        f"Update overlay/50-example/skills/suggesting-bindings/ so agents point at "
        f"current providers."
    )
