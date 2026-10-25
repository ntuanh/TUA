"""Project and global configuration: loading, strict validation, and fingerprints.

A project's configuration lives inside its repository at ``.tua/project.yaml``. Every
relative path in it is resolved against the project root (the repository root) and must
stay inside it. Unknown keys are errors so typos never silently change behaviour.
"""

from __future__ import annotations

import hashlib
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from tua.errors import ConfigError, UnsafePathError
from tua.paths import TuaPaths, resolve_within

CONFIG_SCHEMA_VERSION = 1
PROJECT_CONFIG_RELPATH = Path(".tua") / "project.yaml"
MAX_CONFIG_BYTES = 256 * 1024
SUPPORTED_PROVIDERS = ("claude-code",)
PERMISSION_PROFILES = ("safe", "read-only")
DEFAULT_TIMEOUT_SECONDS = 1800
MAX_TIMEOUT_SECONDS = 24 * 3600
MAX_TURNS_LIMIT = 1000

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\[\]-]{0,99}$")
# Characters that would make a command allowlist entry ambiguous or shell-like.
_COMMAND_FORBIDDEN = re.compile(r"[;&|<>`$(){}*?\\\n\r\t\x00]")
# First tokens that would turn an allowlist entry into "run anything".
_BROAD_COMMANDS = frozenset(
    {
        "sh", "bash", "zsh", "fish", "dash", "ksh", "env", "sudo", "su", "doas", "xargs",
        "eval", "exec", "nohup", "setsid", "command", "builtin", "time", "nice", "timeout",
        "watch", "ssh", "scp", "curl", "wget",
    }
)  # fmt: skip
# Prefixes TUA always denies; allowlist entries may not overlap them.
ALWAYS_DENIED_COMMANDS: tuple[tuple[str, ...], ...] = (
    ("git", "push"),
    ("git", "reset", "--hard"),
    ("git", "clean"),
)


def fingerprint_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class CommandPolicy:
    """Command prefixes (as token tuples) that tasks and evals may or may not run."""

    allow: tuple[tuple[str, ...], ...]
    deny: tuple[tuple[str, ...], ...]

    @staticmethod
    def render(tokens: tuple[str, ...]) -> str:
        return shlex.join(tokens)


@dataclass(frozen=True)
class ProjectConfig:
    name: str
    root: Path
    config_path: Path
    default_provider: str
    default_model: str | None
    timeout_seconds: int
    max_turns: int | None
    permission_profile: str
    skills: tuple[str, ...]
    eval_dir: str
    commands: CommandPolicy
    fingerprint: str

    def skill_path(self, relative: str) -> Path:
        return resolve_within(self.root, relative, what=f"skill {relative!r}")

    @property
    def eval_root(self) -> Path:
        return resolve_within(self.root, self.eval_dir, what="eval_dir")


@dataclass(frozen=True)
class GlobalConfig:
    config_dir: Path
    skills: tuple[str, ...]
    path: Path | None

    def skill_path(self, relative: str) -> Path:
        return resolve_within(self.config_dir, relative, what=f"global skill {relative!r}")


def read_limited(path: Path, *, what: str, limit: int = MAX_CONFIG_BYTES) -> bytes:
    try:
        if not path.is_file():
            raise ConfigError(f"{what} not found: {path}")
        size = path.stat().st_size
        if size > limit:
            raise ConfigError(f"{what} {path} is {size} bytes; the limit is {limit}")
        return path.read_bytes()
    except OSError as exc:
        raise ConfigError(f"cannot read {what} {path}: {exc.strerror}") from exc


def load_yaml_mapping(data: bytes, *, source: Path) -> dict[str, Any]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigError(f"{source} is not valid UTF-8") from exc
    try:
        loaded = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{source} is not valid YAML: {exc}") from exc
    if not isinstance(loaded, dict):
        raise ConfigError(f"{source} must contain a YAML mapping at the top level")
    for key in loaded:
        if not isinstance(key, str):
            raise ConfigError(f"{source}: keys must be strings, got {key!r}")
    return loaded


class _Fields:
    """Small typed accessor that produces precise validation errors."""

    def __init__(self, data: dict[str, Any], source: Path, allowed: set[str]) -> None:
        unknown = sorted(set(data) - allowed)
        if unknown:
            raise ConfigError(
                f"{source}: unknown key(s): {', '.join(unknown)}",
                hint=f"Allowed keys: {', '.join(sorted(allowed))}.",
            )
        self.data = data
        self.source = source

    def error(self, key: str, message: str) -> ConfigError:
        return ConfigError(f"{self.source}: '{key}' {message}")

    def opt_str(self, key: str) -> str | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if not isinstance(value, str) or not value.strip():
            raise self.error(key, "must be a non-empty string")
        return value

    def str_(self, key: str, *, default: str) -> str:
        value = self.opt_str(key)
        return default if value is None else value

    def req_str(self, key: str) -> str:
        value = self.opt_str(key)
        if value is None:
            raise self.error(key, "is required")
        return value

    def opt_int(self, key: str, *, minimum: int, maximum: int) -> int | None:
        if key not in self.data or self.data[key] is None:
            return None
        value = self.data[key]
        if isinstance(value, bool) or not isinstance(value, int):
            raise self.error(key, "must be an integer")
        if not minimum <= value <= maximum:
            raise self.error(key, f"must be between {minimum} and {maximum}")
        return value

    def int_(self, key: str, *, minimum: int, maximum: int, default: int) -> int:
        value = self.opt_int(key, minimum=minimum, maximum=maximum)
        return default if value is None else value

    def str_list(self, key: str) -> list[str]:
        value = self.data.get(key)
        if value is None:
            return []
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise self.error(key, "must be a list of strings")
        return list(value)


def _require_schema_version(fields: _Fields) -> None:
    version = fields.data.get("schema_version")
    if version is None:
        raise fields.error("schema_version", "is required")
    if version != CONFIG_SCHEMA_VERSION:
        raise fields.error(
            "schema_version", f"is {version!r}; this TUA supports {CONFIG_SCHEMA_VERSION}"
        )


def parse_command(entry: str, *, source: Path, key: str) -> tuple[str, ...]:
    """Parse one allow/deny entry into a token prefix, rejecting shell syntax."""
    if not entry.strip() or len(entry) > 200:
        raise ConfigError(f"{source}: '{key}' entries must be 1-200 characters")
    if _COMMAND_FORBIDDEN.search(entry):
        raise ConfigError(
            f"{source}: '{key}' entry {entry!r} contains shell syntax or wildcards",
            hint="Entries are plain command prefixes such as 'python -m pytest'.",
        )
    try:
        tokens = tuple(shlex.split(entry))
    except ValueError as exc:
        raise ConfigError(f"{source}: '{key}' entry {entry!r} cannot be parsed: {exc}") from exc
    if not tokens:
        raise ConfigError(f"{source}: '{key}' entry {entry!r} is empty")
    return tokens


def _starts_with(tokens: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return tokens[: len(prefix)] == prefix


def _parse_commands(fields: _Fields) -> CommandPolicy:
    raw = fields.data.get("commands")
    if raw is None:
        return CommandPolicy(allow=(), deny=())
    if not isinstance(raw, dict):
        raise fields.error("commands", "must be a mapping with 'allow' and/or 'deny' lists")
    sub = _Fields(raw, fields.source, {"allow", "deny"})
    allow = [
        parse_command(e, source=fields.source, key="commands.allow") for e in sub.str_list("allow")
    ]
    deny = [
        parse_command(e, source=fields.source, key="commands.deny") for e in sub.str_list("deny")
    ]
    for tokens in allow:
        rendered = CommandPolicy.render(tokens)
        if tokens[0] in _BROAD_COMMANDS or tokens == ("git",):
            raise ConfigError(
                f"{fields.source}: commands.allow entry '{rendered}' would allow arbitrary "
                "commands",
                hint="Allow specific tools instead, for example 'python -m pytest'.",
            )
        for denied in ALWAYS_DENIED_COMMANDS:
            if _starts_with(tokens, denied) or _starts_with(denied, tokens):
                raise ConfigError(
                    f"{fields.source}: commands.allow entry '{rendered}' overlaps the "
                    f"always-denied command '{CommandPolicy.render(denied)}'"
                )
        for denied in deny:
            if _starts_with(tokens, denied):
                raise ConfigError(
                    f"{fields.source}: commands.allow entry '{rendered}' is also denied by "
                    f"'{CommandPolicy.render(denied)}'"
                )
    if len(set(allow)) != len(allow) or len(set(deny)) != len(deny):
        raise ConfigError(f"{fields.source}: commands.allow/deny contain duplicate entries")
    return CommandPolicy(allow=tuple(allow), deny=tuple(deny))


def _normalize_relpath(value: str, *, root: Path, what: str) -> str:
    resolved = resolve_within(root, value, what=what)
    return resolved.relative_to(root.resolve()).as_posix()


_PROJECT_KEYS = {
    "schema_version",
    "name",
    "repository",
    "default_provider",
    "default_model",
    "timeout_seconds",
    "max_turns",
    "permission_profile",
    "skills",
    "eval_dir",
    "commands",
}


def parse_project_config(data: bytes, *, root: Path, config_path: Path) -> ProjectConfig:
    """Validate raw project configuration bytes. ``root`` must be the repository root."""
    fields = _Fields(load_yaml_mapping(data, source=config_path), config_path, _PROJECT_KEYS)
    _require_schema_version(fields)

    name = fields.req_str("name")
    if not NAME_RE.match(name):
        raise fields.error(
            "name", "must be 1-64 characters of lowercase letters, digits, '.', '_' or '-'"
        )

    repository = fields.str_("repository", default=".")
    try:
        repo_path = resolve_within(root, repository, what="repository")
    except UnsafePathError as exc:
        raise fields.error("repository", str(exc)) from exc
    if repo_path != root.resolve():
        raise fields.error(
            "repository",
            "must be '.' (the directory that contains .tua/); other layouts are not "
            "supported in v0.1",
        )

    provider = fields.str_("default_provider", default="claude-code")
    if provider not in SUPPORTED_PROVIDERS:
        raise fields.error("default_provider", f"must be one of: {', '.join(SUPPORTED_PROVIDERS)}")

    model = fields.opt_str("default_model")
    if model is not None and not MODEL_RE.match(model):
        raise fields.error("default_model", "contains unsupported characters")

    timeout = fields.int_(
        "timeout_seconds", minimum=1, maximum=MAX_TIMEOUT_SECONDS, default=DEFAULT_TIMEOUT_SECONDS
    )
    max_turns = fields.opt_int("max_turns", minimum=1, maximum=MAX_TURNS_LIMIT)

    profile = fields.str_("permission_profile", default="safe")
    if profile not in PERMISSION_PROFILES:
        raise fields.error(
            "permission_profile", f"must be one of: {', '.join(PERMISSION_PROFILES)}"
        )

    skills: list[str] = []
    for entry in fields.str_list("skills"):
        try:
            normalized = _normalize_relpath(entry, root=root, what=f"skill {entry!r}")
        except UnsafePathError as exc:
            raise fields.error("skills", str(exc)) from exc
        if normalized in skills:
            raise fields.error("skills", f"lists {entry!r} more than once")
        skills.append(normalized)

    eval_dir = fields.str_("eval_dir", default=".tua/evals")
    try:
        eval_dir = _normalize_relpath(eval_dir, root=root, what="eval_dir")
    except UnsafePathError as exc:
        raise fields.error("eval_dir", str(exc)) from exc

    return ProjectConfig(
        name=name,
        root=root.resolve(),
        config_path=config_path,
        default_provider=provider,
        default_model=model,
        timeout_seconds=timeout,
        max_turns=max_turns,
        permission_profile=profile,
        skills=tuple(skills),
        eval_dir=eval_dir,
        commands=_parse_commands(fields),
        fingerprint=fingerprint_bytes(data),
    )


def project_config_path(root: Path) -> Path:
    return root / PROJECT_CONFIG_RELPATH


def load_project_config(root: Path) -> ProjectConfig:
    path = project_config_path(root)
    data = read_limited(path, what="project config")
    return parse_project_config(data, root=root, config_path=path)


def load_global_config(paths: TuaPaths) -> GlobalConfig:
    """Load the optional global config. A missing file means "no global skills"."""
    path = paths.global_config_path
    if not path.exists():
        return GlobalConfig(config_dir=paths.config_dir, skills=(), path=None)
    data = read_limited(path, what="global config")
    fields = _Fields(load_yaml_mapping(data, source=path), path, {"schema_version", "skills"})
    _require_schema_version(fields)
    skills: list[str] = []
    for entry in fields.str_list("skills"):
        try:
            normalized = _normalize_relpath(
                entry, root=paths.config_dir, what=f"global skill {entry!r}"
            )
        except UnsafePathError as exc:
            raise fields.error("skills", str(exc)) from exc
        if normalized in skills:
            raise fields.error("skills", f"lists {entry!r} more than once")
        skills.append(normalized)
    return GlobalConfig(config_dir=paths.config_dir, skills=tuple(skills), path=path)
