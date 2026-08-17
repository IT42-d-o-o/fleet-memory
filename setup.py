#!/usr/bin/env python3
"""fleet-memory first-run setup — asks for the mandatory parameters and writes
them to .env immediately, before anything is started.

Usage:
    python setup.py            # interactive wizard -> .env
    python setup.py --force    # overwrite an existing .env

The server and docker-compose both read .env (compose via env_file), so once
this finishes, `docker compose up -d` is fully configured. No parameter is
deferred to "edit the file later": every value the chosen provider requires is
collected, validated non-empty, and persisted on the spot.

Stdlib only. Non-interactive runs (no TTY) exit with instructions instead of
hanging on a prompt.
"""
import argparse
import getpass
import os
import secrets
import stat
import sys

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

PROVIDERS = {
    "1": ("none", "Keyless — fully local, no API key, embeddings on-device (default)"),
    "2": ("openai", "OpenAI — native, needs OPENAI_API_KEY"),
    "3": ("litellm", "LiteLLM — any provider via one key (OpenAI/Anthropic/OpenRouter/...)"),
    "4": ("anthropic", "Anthropic — needs ANTHROPIC_API_KEY (+ local embeddings)"),
    "5": ("ollama", "Ollama — local models, needs a reachable Ollama URL"),
}


def die(msg: str) -> None:
    print(f"error: {msg}", file=sys.stderr)
    sys.exit(1)


def ask(prompt: str, default: str = "", required: bool = False, secret: bool = False) -> str:
    """Prompt until non-empty when required. Secrets are read without echo."""
    suffix = f" [{default}]" if default else ""
    while True:
        raw = (getpass.getpass(f"{prompt}{suffix}: ") if secret
               else input(f"{prompt}{suffix}: ")).strip()
        if not raw and default:
            return default
        if raw or not required:
            return raw
        print("  this value is mandatory — it cannot be left empty.")


def choose_provider() -> str:
    print("\nLLM provider — how should memories be embedded/processed?")
    for k, (name, desc) in PROVIDERS.items():
        print(f"  {k}) {name:<10} {desc}")
    while True:
        c = input("choice [1]: ").strip() or "1"
        if c in PROVIDERS:
            return PROVIDERS[c][0]
        print("  pick 1-5.")


def collect() -> dict:
    """The wizard: every mandatory parameter for the chosen configuration."""
    cfg: dict = {}
    provider = choose_provider()
    cfg["LLM_PROVIDER"] = provider

    if provider == "openai":
        cfg["OPENAI_API_KEY"] = ask("OpenAI API key", required=True, secret=True)
    elif provider == "litellm":
        cfg["LLM_API_KEY"] = ask("Provider API key", required=True, secret=True)
        cfg["MEM0_LLM_MODEL"] = ask("Model (provider/model)", default="openai/gpt-4o-mini")
    elif provider == "anthropic":
        cfg["ANTHROPIC_API_KEY"] = ask("Anthropic API key", required=True, secret=True)
        cfg["MEM0_LLM_MODEL"] = ask("Model", default="claude-3-5-haiku-20241022")
        emb = ask("Embeddings: [1] OpenAI key / [2] Ollama URL", default="2")
        if emb.strip() == "1":
            cfg["OPENAI_API_KEY"] = ask("OpenAI API key (embeddings)", required=True, secret=True)
        else:
            cfg["OLLAMA_URL"] = ask("Ollama URL", default="http://host.docker.internal:11434")
    elif provider == "ollama":
        cfg["OLLAMA_URL"] = ask("Ollama URL", default="http://host.docker.internal:11434")

    print("\nMCP bearer auth — required for any deployment reachable by others.")
    tok = ask("Auth token: [g]enerate / [n]one (dev only) / paste your own", default="g")
    if tok.lower() == "g":
        cfg["MCP_AUTH_TOKEN"] = secrets.token_urlsafe(32)
        print("  generated a 43-char token (written to .env, not displayed).")
    elif tok.lower() != "n":
        cfg["MCP_AUTH_TOKEN"] = tok
    return cfg


def write_env(cfg: dict) -> None:
    lines = ["# fleet-memory site config — written by setup.py. Not for git.\n"]
    lines += [f"{k}={v}\n" for k, v in cfg.items()]
    with open(ENV_PATH, "w", encoding="utf-8") as fh:
        fh.writelines(lines)
    if os.name != "nt":
        os.chmod(ENV_PATH, stat.S_IRUSR | stat.S_IWUSR)  # 600 — may hold keys


def main() -> None:
    ap = argparse.ArgumentParser(
        description="fleet-memory first-run setup — collects mandatory parameters into .env")
    ap.add_argument("--force", action="store_true", help="overwrite an existing .env")
    args = ap.parse_args()

    if os.path.exists(ENV_PATH) and not args.force:
        die(f"{ENV_PATH} already exists — rerun with --force to overwrite it.")
    if not sys.stdin.isatty():
        die("no TTY: this wizard is interactive. Either run it in a terminal, "
            "or copy .env.example to .env and fill in the values it documents.")

    print("fleet-memory setup — mandatory parameters are collected now and "
          "saved immediately;\nnothing is left to configure after this.")
    cfg = collect()
    write_env(cfg)

    shown = {k: (v[:4] + "****" if ("KEY" in k or "TOKEN" in k) and v else v)
             for k, v in cfg.items()}
    print(f"\nwrote {ENV_PATH}:")
    for k, v in shown.items():
        print(f"  {k}={v}")
    print("\nnext: docker compose up -d")


if __name__ == "__main__":
    main()
