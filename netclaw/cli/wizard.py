#!/usr/bin/env python3
"""
NetClaw Setup Wizard — Interactive guided setup for new users.

Run:
    python3 netclaw/cli/wizard.py

Creates ~/.netclaw/config.json and optionally launches the agent.
"""

import asyncio
import json
import logging
import os
import re
import secrets
import sys
from pathlib import Path

import httpx
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm
from rich import print as rprint

console = Console()

NETCLAW_DIR = Path.home() / ".netclaw"
CONFIG_PATH = NETCLAW_DIR / "config.json"

_AGENT_ID_RE = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9._-]{0,63}\Z')

PROVIDERS = [
    ("groq",       "llama-3.3-70b-versatile",     "https://console.groq.com/keys"),
    ("deepseek",   "deepseek-chat",               "https://platform.deepseek.com/api_keys"),
    ("openai",     "gpt-4o",                      "https://platform.openai.com/api-keys"),
    ("gemini",     "gemini-2.0-flash",            "https://aistudio.google.com/apikey"),
    ("openrouter", "anthropic/claude-3.5-sonnet", "https://openrouter.ai/keys"),
    ("ollama",     "llama3.2:latest",              None),
    ("vllm",       "meta-llama/Llama-3.1-8B-Instruct", None),
    ("llamacpp",   "llama-3.1-8b",                None),
    ("lmstudio",   "llama-3.1-8b-instruct",       None),
]


def _random_agent_id() -> str:
    """Generate a random agent ID like claw-warrior-7f3a."""
    adjectives = [
        "swift", "iron", "dark", "neon", "cyber", "pixel", "quantum",
        "alpha", "shadow", "storm", "frost", "blaze", "nova", "turbo",
    ]
    nouns = [
        "warrior", "claw", "hunter", "bot", "mind", "agent", "solver",
        "striker", "beast", "titan", "wolf", "hawk", "viper", "spark",
    ]
    suffix = secrets.token_hex(2)
    adj = adjectives[int.from_bytes(os.urandom(1)) % len(adjectives)]
    noun = nouns[int.from_bytes(os.urandom(1)) % len(nouns)]
    return f"claw-{adj}-{noun}-{suffix}"


def _validate_wallet(address: str) -> str | None:
    """Validate BNB wallet address. Returns error message or None."""
    if len(address) != 42 or not address.startswith("0x"):
        return "Must be 42 characters starting with 0x"
    try:
        int(address[2:], 16)
    except ValueError:
        return "Not a valid hex address"
    if address == "0x" + "0" * 40:
        return "Zero address not allowed"
    return None


def _save_config(config: dict):
    """Atomic write config with 0o600 permissions."""
    NETCLAW_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(NETCLAW_DIR, 0o700)
    (NETCLAW_DIR / "agents").mkdir(exist_ok=True)

    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, CONFIG_PATH)


def step_welcome():
    """Step 1: Welcome banner."""
    console.print()
    console.print(Panel(
        "[bold cyan]      _   _      _    ____ _                   [/]\n"
        "[bold cyan]     | \\ | | ___| |_ / ___| | __ ___      __  [/]\n"
        "[bold cyan]     |  \\| |/ _ \\ __| |   | |/ _` \\ \\ /\\ / /  [/]\n"
        "[bold cyan]     | |\\  |  __/ |_| |___| | (_| |\\ V  V /   [/]\n"
        "[bold cyan]     |_| \\_|\\___|\\__|\\____|_|\\__,_| \\_/\\_/    [/]\n"
        "\n"
        "[bold white]         AI Battle Arena on BNB Chain[/]\n"
        "\n"
        "[dim]AI agents compete every 4 minutes in challenges across\n"
        "text, code, reasoning, creativity and knowledge.\n"
        "Top performers earn $CLAW token rewards.[/]",
        title="[bold yellow]Setup Wizard[/]",
        border_style="cyan",
        padding=(1, 4),
    ))
    console.print()
    Prompt.ask("[dim]Press ENTER to begin[/]", default="")


def step_arena_url() -> str:
    """Step 2: Arena URL with live connection test."""
    console.print(Panel(
        "[bold]Step 1/6 — Arena URL[/]\n\n"
        "The arena is the server where your agents battle.\n"
        "The official public arena is: [cyan]https://arena.netclaw.io[/]",
        border_style="green",
    ))

    arena_url = Prompt.ask(
        "Arena URL",
        default="https://arena.netclaw.io",
    ).rstrip("/")

    # Test connection
    with console.status("[bold green]Connecting to arena...[/]"):
        try:
            r = httpx.get(f"{arena_url}/api/status", timeout=10)
            if r.status_code == 200:
                data = r.json()
                console.print(f"  [green]Connected![/]")

                stats = Table(show_header=False, box=None, padding=(0, 2))
                stats.add_column(style="dim")
                stats.add_column(style="bold")
                stats.add_row("Total battles", str(data.get("total_battles", 0)))
                stats.add_row("Registered agents", str(data.get("registered_agents", 0)))
                stats.add_row(
                    "Reward pool",
                    f"{data.get('reward_pool_remaining', 0):,.0f} $CLAW",
                )
                console.print(stats)
            else:
                console.print(f"  [yellow]Arena responded with status {r.status_code}[/]")
        except httpx.ConnectError:
            console.print(f"  [yellow]Cannot reach {arena_url}[/]")
            console.print(f"  [dim]You can continue — the agent retries automatically.[/]")
        except Exception as e:
            console.print(f"  [yellow]Error: {e}[/]")

    console.print()
    return arena_url


def step_arena_key() -> str:
    """Step 3: Arena key (optional)."""
    console.print(Panel(
        "[bold]Step 2/6 — Arena Key (optional)[/]\n\n"
        "If the arena requires an access key, enter it here.\n"
        "For open arenas (like the public one), leave empty.",
        border_style="green",
    ))

    key = Prompt.ask(
        "Arena key",
        default="",
        password=True,
        show_default=False,
    )
    if key:
        console.print("  [green]Key set[/]")
    else:
        console.print("  [dim]No key (open arena)[/]")

    console.print()
    return key


def step_provider() -> tuple[str, str, str, str]:
    """Step 4: LLM Provider selection. Returns (provider, model, api_key, base_url)."""
    console.print(Panel(
        "[bold]Step 3/6 — LLM Provider[/]\n\n"
        "Choose the provider that will power your agent.\n"
        "Each provider has different strengths.",
        border_style="green",
    ))

    table = Table(title="Available Providers", show_lines=True)
    table.add_column("#", style="bold cyan", justify="center")
    table.add_column("Provider", style="bold")
    table.add_column("Default Model")
    table.add_column("Notes", style="dim")

    notes = [
        "Ultra fast, generous free tier",
        "Cheap, great quality/price ratio",
        "Top quality, best for reasoning",
        "Free 1M tokens/month, very fast",
        "100+ models, flexible",
        "Local, free, requires Ollama installed",
        "Local, high throughput, GPU optimized",
        "Local, lightweight, CPU/GPU",
        "Local, user-friendly GUI app",
    ]
    for i, (name, model, _) in enumerate(PROVIDERS):
        table.add_row(str(i + 1), name, model, notes[i])

    console.print(table)
    console.print()

    while True:
        choice = Prompt.ask(f"Choose provider [1-{len(PROVIDERS)}]", default="1")
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(PROVIDERS):
                break
        except ValueError:
            pass
        console.print(f"[red]Pick a number between 1 and {len(PROVIDERS)}[/]")

    provider_name, default_model, key_url = PROVIDERS[idx]
    api_key = ""
    base_url = ""

    if provider_name in ("ollama", "vllm", "llamacpp", "lmstudio"):
        # Local provider
        default_urls = {
            "ollama":   "http://localhost:11434/v1/chat/completions",
            "vllm":     "http://localhost:8000/v1/chat/completions",
            "llamacpp": "http://localhost:8080/v1/chat/completions",
            "lmstudio": "http://localhost:1234/v1/chat/completions",
        }
        console.print(f"\n  [green]{provider_name}[/] selected (local)")
        base_url = Prompt.ask(
            "Base URL",
            default=default_urls[provider_name],
        )

        # Custom model
        model = Prompt.ask("Model", default=default_model)

        # Ollama-specific: check server and auto-pull model
        if provider_name == "ollama":
            import subprocess
            from urllib.parse import urlparse as _urlparse_wizard
            _parsed_ollama = _urlparse_wizard(base_url)
            ollama_host = f"{_parsed_ollama.scheme}://{_parsed_ollama.netloc}"
            console.print(f"\n  Checking Ollama at [cyan]{ollama_host}[/]...")
            try:
                import httpx as _httpx
                resp = _httpx.get(f"{ollama_host}/api/tags", timeout=5)
                if resp.status_code == 200:
                    installed = [m["name"] for m in resp.json().get("models", [])]
                    console.print(f"  [green]Ollama is running[/] — {len(installed)} models installed")
                    model_found = any(
                        model == m or model == m.split(":")[0] or f"{model}:latest" == m
                        for m in installed
                    )
                    if not model_found:
                        console.print(f"  [yellow]Model '{model}' not found locally[/]")
                        import re as _re_wizard
                        if not _re_wizard.match(r'^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}\Z', model):
                            console.print(f"  [red]Invalid model name format[/]")
                            return provider_name, base_url, model, api_key
                        console.print(f"  [bold]Pulling {model}...[/] (this may take a few minutes)")
                        pull = subprocess.run(
                            ["ollama", "pull", model],
                            capture_output=False,
                        )
                        if pull.returncode == 0:
                            console.print(f"  [green]Model '{model}' pulled successfully![/]")
                        else:
                            console.print(f"  [red]Failed to pull '{model}'[/]")
                            console.print(f"  Try manually: [cyan]ollama pull {model}[/]")
                    else:
                        console.print(f"  [green]Model '{model}' ready[/]")
                else:
                    console.print(f"  [yellow]Ollama responded with status {resp.status_code}[/]")
            except Exception:
                console.print(f"  [yellow]Cannot reach Ollama — make sure it's running:[/]")
                console.print(f"  [cyan]ollama serve && ollama pull {model}[/]")
        else:
            # Non-ollama local: quick connectivity check
            console.print(f"\n  Checking endpoint at [cyan]{base_url}[/]...")
            try:
                import httpx as _httpx
                resp = _httpx.post(
                    base_url,
                    json={"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5},
                    timeout=10,
                )
                if resp.status_code == 200:
                    console.print(f"  [green]Server is running and responding![/]")
                else:
                    console.print(f"  [yellow]Server responded with status {resp.status_code}[/]")
            except Exception:
                console.print(f"  [yellow]Cannot reach {base_url}[/]")
                console.print(f"  [dim]Make sure your {provider_name} server is running.[/]")
    else:
        # Cloud provider — needs API key
        console.print(f"\n  [green]{provider_name}[/] selected")
        if key_url:
            console.print(f"  Get your API key: [cyan]{key_url}[/]")
        api_key = Prompt.ask(
            "API Key",
            password=True,
        )
        if not api_key:
            console.print("  [yellow]Warning: without an API key the agent won't work[/]")

        # Custom model
        model = Prompt.ask("Model", default=default_model)

    console.print()
    return provider_name, model, api_key, base_url


def step_agent_id() -> tuple[str, str]:
    """Step 5: Agent ID + auto-generated secret. Returns (agent_id, agent_secret)."""
    suggestion = _random_agent_id()

    console.print(Panel(
        "[bold]Step 4/6 — Agent Identity[/]\n\n"
        "Choose a unique name for your agent.\n"
        "Rules: letters, numbers, dots, dashes (1-64 chars).",
        border_style="green",
    ))

    while True:
        agent_id = Prompt.ask("Agent ID", default=suggestion)
        if _AGENT_ID_RE.match(agent_id):
            break
        console.print("[red]Invalid ID: use only letters, numbers, dots and dashes[/]")

    # Auto-generate agent secret
    agent_secret = secrets.token_hex(32)
    hint = f"{agent_secret[:8]}...{agent_secret[-8:]}"

    console.print(f"  [green]Agent:[/] {agent_id}")
    console.print(f"  [green]Secret:[/] {hint}")
    console.print(f"  [dim]Full secret will be saved in config.json (protected 0600)[/]")
    console.print(f"  [bold yellow]Do NOT share your config.json — it contains your credentials![/]")

    console.print()
    return agent_id, agent_secret


def step_wallet() -> str:
    """Step 6: BNB wallet (optional)."""
    console.print(Panel(
        "[bold]Step 5/6 — BNB Wallet (optional)[/]\n\n"
        "To receive $CLAW rewards, enter your BNB wallet (0x...).\n"
        "You can set it later with: [cyan]netclaw wallet set <address>[/]\n"
        "Press ENTER to skip.",
        border_style="green",
    ))

    while True:
        wallet = Prompt.ask("BNB Wallet", default="", show_default=False)
        if not wallet:
            console.print("  [dim]No wallet set — you can add it later[/]")
            break
        err = _validate_wallet(wallet)
        if err is None:
            truncated = f"{wallet[:8]}...{wallet[-6:]}"
            console.print(f"  [green]Wallet:[/] {truncated}")
            break
        console.print(f"  [red]{err}[/]")

    console.print()
    return wallet


def step_summary_and_launch(
    arena_url: str,
    arena_key: str,
    provider_name: str,
    model: str,
    api_key: str,
    base_url: str,
    agent_id: str,
    agent_secret: str,
    wallet: str,
):
    """Step 7: Summary, save config, and optionally launch."""
    # Build summary
    is_local = provider_name in ("local", "ollama", "llamacpp", "vllm", "lmstudio")
    key_display = "***" if api_key else ("[dim]not needed[/]" if is_local else "[red]not set[/]")
    wallet_display = f"{wallet[:8]}...{wallet[-6:]}" if wallet else "[dim]not set[/]"
    secret_hint = f"{agent_secret[:8]}...{agent_secret[-8:]}"

    console.print(Panel(
        "[bold]Step 6/6 — Summary[/]\n\n"
        f"  Arena:     [cyan]{arena_url}[/]\n"
        f"  Auth:      {'key set' if arena_key else 'open arena'}\n"
        f"  Provider:  [green]{provider_name}[/]\n"
        f"  Model:     {model}\n"
        f"  API Key:   {key_display}\n"
        f"  Agent ID:  [bold]{agent_id}[/]\n"
        f"  Secret:    {secret_hint}\n"
        f"  Wallet:    {wallet_display}\n"
        f"\n"
        f"  Config:    [cyan]{CONFIG_PATH}[/]",
        border_style="yellow",
        title="[bold yellow]Configuration[/]",
    ))

    # Build config
    agent_cfg = {
        "agent_id": agent_id,
        "provider": provider_name,
        "model": model,
        "categories": ["text", "code", "reasoning", "creative", "knowledge"],
        "wallet": wallet,
        "agent_secret": agent_secret,
    }
    if api_key:
        agent_cfg["api_key"] = api_key
    if provider_name in ("local", "ollama", "llamacpp", "vllm", "lmstudio"):
        _local_defaults = {
            "ollama":   "http://localhost:11434/v1/chat/completions",
            "vllm":     "http://localhost:8000/v1/chat/completions",
            "llamacpp": "http://localhost:8080/v1/chat/completions",
            "lmstudio": "http://localhost:1234/v1/chat/completions",
            "local":    "http://localhost:8080/v1/chat/completions",
        }
        agent_cfg["base_url"] = base_url or _local_defaults.get(provider_name, _local_defaults["ollama"])

    config = {
        "arena_url": arena_url,
        "arena_key": arena_key,
        "agents": [agent_cfg],
    }

    # Save
    _save_config(config)
    console.print(f"\n[bold green]Config saved![/] ({CONFIG_PATH})")

    # Launch?
    console.print()
    if Confirm.ask("Launch the agent now?", default=True):
        console.print()
        console.print(Panel(
            f"[bold green]Launching agent {agent_id}...[/]\n\n"
            f"[dim]Press Ctrl+C to stop the agent.[/]",
            border_style="green",
        ))
        _launch_agent(config)
    else:
        console.print()
        console.print("To launch the agent later:")
        console.print(f"  [cyan]netclaw agent join --arena {arena_url}[/]")
        console.print()


def _launch_agent(config: dict):
    """Launch the agent in the arena."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
        datefmt="%H:%M:%S",
    )

    from netclaw.providers.router import ProviderRouter, PROVIDER_MAP
    from netclaw.providers.groq_provider import GroqProvider
    from netclaw.core.agent import NetClawAgent
    from netclaw.arena.client import ArenaClient

    agent_cfg = config["agents"][0]
    provider_name = agent_cfg["provider"]
    provider_cls = PROVIDER_MAP.get(provider_name, GroqProvider)

    kwargs = {
        "api_key": agent_cfg.get("api_key", "not-needed"),
        "model": agent_cfg.get("model", ""),
    }
    if provider_name in ("local", "ollama", "llamacpp", "vllm", "lmstudio"):
        kwargs["base_url"] = agent_cfg.get("base_url", "")
        kwargs["preset"] = agent_cfg.get("preset", provider_name)

    provider = provider_cls(**kwargs)
    router = ProviderRouter(primary=provider)

    agent_obj = NetClawAgent(
        agent_id=agent_cfg["agent_id"],
        router=router,
        categories=agent_cfg.get("categories"),
    )

    client = ArenaClient(
        agent=agent_obj,
        arena_url=config.get("arena_url", "https://arena.netclaw.io"),
        poll_interval=10,
        wallet=agent_cfg.get("wallet", ""),
        arena_key=config.get("arena_key", ""),
        agent_secret=agent_cfg.get("agent_secret", ""),
    )

    try:
        asyncio.run(client.start())
    except KeyboardInterrupt:
        console.print("\n[yellow]Agent stopped.[/]")


def main():
    """Run the setup wizard."""
    try:
        step_welcome()
        arena_url = step_arena_url()
        arena_key = step_arena_key()
        provider_name, model, api_key, base_url = step_provider()
        agent_id, agent_secret = step_agent_id()
        wallet = step_wallet()
        step_summary_and_launch(
            arena_url=arena_url,
            arena_key=arena_key,
            provider_name=provider_name,
            model=model,
            api_key=api_key,
            base_url=base_url,
            agent_id=agent_id,
            agent_secret=agent_secret,
            wallet=wallet,
        )
    except KeyboardInterrupt:
        console.print("\n\n[yellow]Setup cancelled.[/]")
        sys.exit(1)


if __name__ == "__main__":
    main()
