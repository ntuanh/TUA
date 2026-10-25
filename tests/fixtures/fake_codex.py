# A stand-in for the OpenAI Codex CLI used by TUA's tests. It mimics the behaviours TUA
# relies on (observed with codex-cli 0.164.0) and never contacts any service.
#
# Control file (JSON, path in FAKE_CODEX_CONTROL):
#   mode: success | model_error | rate_limit | crash | invalid_output | hang
#   version, logged_in, unsupported_flags, write_files {relpath: text}, result, started_file
# Every exec invocation with a prompt is appended to FAKE_CODEX_LOG as one JSON line.
import hashlib
import json
import os
import sys
import time

FLAGS_WITH_VALUE = {
    "--sandbox",
    "-s",
    "--cd",
    "-C",
    "--output-last-message",
    "-o",
    "--model",
    "-m",
    "-c",
    "--config",
}
BOOL_FLAGS = {
    "--json",
    "--ignore-user-config",
    "--ignore-rules",
    "--strict-config",
    "--ephemeral",
    "--skip-git-repo-check",
}
KNOWN_KEYS = {
    "approval_policy",
    "sandbox_workspace_write.network_access",
    "developer_instructions",
    "model_reasoning_effort",
}


def control():
    path = os.environ.get("FAKE_CODEX_CONTROL")
    if path and os.path.exists(path):
        with open(path) as handle:
            return json.load(handle)
    return {}


def emit(event):
    sys.stdout.write(json.dumps(event) + "\n")
    sys.stdout.flush()


def parse(argv, unsupported):
    opts = {"-c": []}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "-":
            i += 1
            continue
        if arg in unsupported or (arg.startswith("-") and arg not in FLAGS_WITH_VALUE | BOOL_FLAGS):
            sys.stderr.write(f"error: unexpected argument '{arg}' found\n")
            sys.exit(2)
        if arg in FLAGS_WITH_VALUE:
            if arg in ("-c", "--config"):
                opts["-c"].append(argv[i + 1])
            else:
                opts[arg] = argv[i + 1]
            i += 2
        else:
            opts[arg] = True
            i += 1
    return opts


def main():
    ctl = control()
    argv = sys.argv[1:]
    if argv[:1] == ["--version"]:
        print(f"codex-cli {ctl.get('version', '0.164.0')}")
        return
    if argv[:2] == ["login", "status"]:
        if ctl.get("logged_in", True):
            print("Logged in using ChatGPT")
            return
        print("Not logged in")
        sys.exit(1)
    if argv[:1] != ["exec"]:
        sys.stderr.write("fake codex: only exec is supported\n")
        sys.exit(2)

    opts = parse(argv[1:], set(ctl.get("unsupported_flags", [])))
    config = {}
    for item in opts["-c"]:
        key, _, value = item.partition("=")
        if key not in KNOWN_KEYS:
            sys.stderr.write(
                f"Error loading config.toml: unknown configuration field `{key}` "
                "in -c/--config override\n"
            )
            sys.exit(1)
        config[key] = value
    sys.stderr.write("Reading prompt from stdin...\n")
    prompt = sys.stdin.read()
    if not prompt:
        sys.stderr.write("No prompt provided via stdin.\n")
        sys.exit(1)

    instructions = json.loads(config.get("developer_instructions", '""'))
    workdir = opts.get("--cd") or os.getcwd()
    sandbox = opts.get("--sandbox")
    log = os.environ.get("FAKE_CODEX_LOG")
    if log:
        with open(log, "a") as handle:
            handle.write(
                json.dumps(
                    {
                        "argv": argv,
                        "cwd": os.getcwd(),
                        "workdir": workdir,
                        "prompt": prompt,
                        "sandbox": sandbox,
                        "config": config,
                        "instructions_sha256": "sha256:"
                        + hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
                        "env_has_claudecode": "CLAUDECODE" in os.environ,
                    }
                )
                + "\n"
            )

    if ctl.get("started_file"):
        with open(ctl["started_file"], "w") as handle:
            handle.write(str(os.getpid()))
    mode = ctl.get("mode", "success")
    emit({"type": "thread.started", "thread_id": "11111111-1111-4111-8111-111111111111"})
    emit({"type": "turn.started"})
    if mode == "success":
        written = []
        if sandbox == "workspace-write":
            for rel, text in ctl.get(
                "write_files", {"hello.txt": "hello from fake codex\n"}
            ).items():
                target = os.path.join(workdir, rel)
                os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
                with open(target, "w") as handle:
                    handle.write(text)
                written.append(rel)
        message = ctl.get("result", "Done." if written else "Read-only sandbox: wrote nothing.")
        emit(
            {
                "type": "item.completed",
                "item": {"id": "item_0", "type": "agent_message", "text": message},
            }
        )
        emit(
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 100, "cached_input_tokens": 0, "output_tokens": 20},
            }
        )
        if opts.get("--output-last-message"):
            with open(opts["--output-last-message"], "w") as handle:
                handle.write(message)
        return
    if mode in ("model_error", "rate_limit"):
        status, code, text = (
            (
                400,
                "model_not_found",
                "The 'gpt-x' model is not supported when using Codex with a ChatGPT account.",
            )
            if mode == "model_error"
            else (429, "rate_limit_exceeded", "You've hit your usage limit.")
        )
        inner = json.dumps(
            {
                "type": "error",
                "status": status,
                "error": {"type": "invalid_request_error", "message": text, "code": code},
            }
        )
        emit({"type": "error", "message": inner})
        emit({"type": "turn.failed", "error": {"message": inner}})
        sys.exit(1)
    if mode == "crash":
        sys.stderr.write("thread 'main' panicked\n")
        sys.exit(101)
    if mode == "invalid_output":
        sys.stdout.write("not an event stream\n")
        return
    if mode == "hang":
        time.sleep(300)
        return
    sys.stderr.write(f"fake codex: unknown mode {mode}\n")
    sys.exit(2)


main()
