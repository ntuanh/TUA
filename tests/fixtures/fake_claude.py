# A stand-in for the Claude Code CLI used by TUA's tests. It mimics the behaviours TUA
# relies on (observed with Claude Code 2.1.301) and never contacts any service.
#
# Control file (JSON, path in FAKE_CLAUDE_CONTROL):
#   mode: success | provider_error | auth_error | rate_limit | crash | invalid_output |
#         hang | ignore_sigterm | denials
#   version, logged_in, unsupported_flags, write_files {relpath: text}, result,
#   grandchild_pid_file, started_file
# Every --print invocation is appended to FAKE_CLAUDE_LOG as one JSON line.
import hashlib
import json
import os
import signal
import subprocess
import sys
import time

KNOWN_FLAGS = {
    "--print": 0,
    "-p": 0,
    "--output-format": 1,
    "--append-system-prompt-file": 1,
    "--permission-mode": 1,
    "--permission-prompts": 1,
    "--settings": 1,
    "--setting-sources": 1,
    "--strict-mcp-config": 0,
    "--model": 1,
    "--max-turns": 1,
    "--disallowedTools": -1,  # variadic
}
PERMISSION_MODES = ["acceptEdits", "auto", "bypassPermissions", "manual", "dontAsk", "plan"]


def control():
    path = os.environ.get("FAKE_CLAUDE_CONTROL")
    if path and os.path.exists(path):
        with open(path) as handle:
            return json.load(handle)
    return {}


def emit(obj, code=0):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()
    sys.exit(code)


def result(text, *, is_error=False, subtype="success", denials=None):
    return {
        "type": "result",
        "subtype": subtype,
        "is_error": is_error,
        "duration_ms": 12,
        "num_turns": 2,
        "result": text,
        "session_id": "00000000-0000-4000-8000-000000000000",
        "total_cost_usd": 0.0123,
        "usage": {"input_tokens": 10, "output_tokens": 20},
        "modelUsage": {"claude-fake-model-1": {"inputTokens": 10, "outputTokens": 20}},
        "permission_denials": denials or [],
    }


def parse(argv, unsupported):
    opts = {}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in unsupported or (arg.startswith("-") and arg not in KNOWN_FLAGS):
            sys.stderr.write(f"error: unknown option '{arg}'\n")
            sys.exit(1)
        arity = KNOWN_FLAGS.get(arg)
        if arity == 0:
            opts[arg] = True
            i += 1
        elif arity == 1:
            opts[arg] = argv[i + 1]
            i += 2
        else:
            values = []
            i += 1
            while i < len(argv) and not argv[i].startswith("-"):
                values.append(argv[i])
                i += 1
            opts[arg] = values
    return opts


def main():
    ctl = control()
    argv = sys.argv[1:]
    if argv[:1] in (["--version"], ["-v"]):
        print(f"{ctl.get('version', '2.1.301')} (Claude Code)")
        return
    if argv[:2] == ["auth", "status"]:
        # Includes account details on purpose: TUA must only ever read loggedIn.
        print(
            json.dumps(
                {
                    "loggedIn": ctl.get("logged_in", True),
                    "authMethod": "fake",
                    "email": "leak-canary@example.invalid",
                    "orgName": "Leak Canary Org",
                }
            )
        )
        return

    opts = parse(argv, set(ctl.get("unsupported_flags", [])))
    mode = opts.get("--permission-mode")
    if mode is not None and mode not in PERMISSION_MODES:
        sys.stderr.write(
            f"error: option '--permission-mode <mode>' argument '{mode}' is invalid.\n"
        )
        sys.exit(1)
    append = opts.get("--append-system-prompt-file")
    if append is not None and not os.path.exists(append):
        sys.stderr.write(f"Error: Append system prompt file not found: {append}\n")
        sys.exit(1)
    prompt = sys.stdin.read()
    if not prompt:
        sys.stderr.write(
            "Error: Input must be provided either through stdin or as a prompt "
            "argument when using --print\n"
        )
        sys.exit(1)

    with open(append, "rb") as handle:
        appended = handle.read()
    settings_path = opts.get("--settings")
    settings = None
    if settings_path and os.path.exists(settings_path):
        with open(settings_path) as handle:
            settings = json.load(handle)
    record = {
        "argv": argv,
        "cwd": os.getcwd(),
        "prompt": prompt,
        "append_sha256": "sha256:" + hashlib.sha256(appended).hexdigest(),
        "settings": settings,
        "env_has_claudecode": "CLAUDECODE" in os.environ,
        "env_has_messaging_token": "CLAUDE_CODE_MESSAGING_TOKEN" in os.environ,
        "env_git_dir": os.environ.get("GIT_DIR"),
        "pid": os.getpid(),
    }
    log = os.environ.get("FAKE_CLAUDE_LOG")
    if log:
        with open(log, "a") as handle:
            handle.write(json.dumps(record) + "\n")

    run_mode = ctl.get("mode", "success")
    if ctl.get("started_file"):
        with open(ctl["started_file"], "w") as handle:
            handle.write(str(os.getpid()))
    if run_mode in ("success", "denials"):
        for rel, text in ctl.get("write_files", {"hello.txt": "hello from fake claude\n"}).items():
            target = os.path.join(os.getcwd(), rel)
            os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
            with open(target, "w") as handle:
                handle.write(text)
        denials = None
        if run_mode == "denials":
            denials = [{"tool_name": "Bash", "tool_use_id": "t1", "tool_input": {"command": "x"}}]
        emit(result(ctl.get("result", "Created hello.txt."), denials=denials))
    if run_mode == "provider_error":
        emit(result("Something went wrong", is_error=True, subtype="error_during_execution"), 1)
    if run_mode == "auth_error":
        emit(result("Invalid API key · Please run /login", is_error=True), 1)
    if run_mode == "rate_limit":
        emit(result("Claude AI usage limit reached|1700000000", is_error=True), 1)
    if run_mode == "crash":
        sys.stderr.write("fatal: simulated crash\n")
        sys.exit(3)
    if run_mode == "invalid_output":
        print("this is not json")
        return
    if run_mode in ("hang", "ignore_sigterm"):
        if run_mode == "ignore_sigterm":
            signal.signal(signal.SIGTERM, signal.SIG_IGN)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(300)"])
        if ctl.get("grandchild_pid_file"):
            with open(ctl["grandchild_pid_file"], "w") as handle:
                handle.write(str(child.pid))
        time.sleep(300)
        return
    sys.stderr.write(f"fake claude: unknown mode {run_mode}\n")
    sys.exit(2)


main()
