from __future__ import annotations

from tua.gitsetup import escape_pattern, ignore_patterns_for, looks_secret


def test_ignore_patterns_prefer_build_folders() -> None:
    assert ignore_patterns_for(
        ["sw/drivers/build/SOC/app.elf", "sw/drivers/build/SOC/app.dis", "notes/todo.txt"]
    ) == ["/sw/drivers/build/", "/notes/todo.txt"]


def test_escape_pattern() -> None:
    assert escape_pattern("a b.txt") == "/a b.txt"
    assert escape_pattern("weird*[1].txt") == "/weird\\*\\[1].txt"
    assert escape_pattern("#notes") == "/\\#notes"


def test_looks_secret() -> None:
    for name in (".env", ".env.local", "server.pem", "id_rsa", "aws_credentials.json", ".netrc"):
        assert looks_secret(f"dir/{name}")
    for name in (".env.example", "main.c", "keyboard.py"):
        assert not looks_secret(name)


def test_build_folders_with_tracked_files_hide_only_the_files() -> None:
    tracked = frozenset({"sw/build/SOC/uart_hello.elf", "sw/build/SOC/.gitkeep"})
    assert ignore_patterns_for(["sw/build/SOC/adc_app.elf"], tracked) == [
        "/sw/build/SOC/adc_app.elf"
    ]
