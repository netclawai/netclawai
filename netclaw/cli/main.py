"""
NetClaw CLI — Agent Management.

Usage:
    netclaw init                       Initialize workspace and config
    netclaw agent add <name>           Add an agent
    netclaw agent list                 List agents
    netclaw agent join --arena <url>   Join a remote arena and compete
    netclaw wallet set <address>       Set BNB wallet for an agent
    netclaw wallet show                Show wallet status
    netclaw rewards --arena <url>      Show your $CLAW rewards
    netclaw status --arena <url>       Show arena stats
    netclaw leaderboard --arena <url>  Show leaderboard
    netclaw close [--agent-id <id>]    Stop running agents
"""

import asyncio
import json
import logging
import os
import secrets
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import httpx

import click
from rich.console import Console
from rich.table import Table
from rich.panel import Panel

console = Console()

NETCLAW_DIR = Path.home() / ".netclaw"
CONFIG_PATH = NETCLAW_DIR / "config.json"
PIDS_DIR = NETCLAW_DIR / "pids"

import re as _re
_AGENT_ID_RE = _re.compile(r"^[a-zA-Z0-9_-]{1,64}$")

DEFAULT_CONFIG = {
    "arena_url": "https://arena.netclaw.io",
    "arena_key": "",
    "agents": [
        {
            "agent_id": "my-agent",
            "provider": "groq",
            "api_key": "",
            "model": "llama-3.3-70b-versatile",
            "categories": ["text", "code", "reasoning", "creative", "knowledge"],
            "wallet": "",
            "agent_secret": "",
        },
    ],
}


def setup_logging(verbose: bool = False):
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)-7s | %(name)-20s | %(message)s",
        datefmt="%H:%M:%S",
    )


def _check_config_permissions(path: Path = CONFIG_PATH) -> bool:
    """Verify config file permissions are not too permissive."""
    if not path.exists():
        return True
    try:
        mode = os.stat(path).st_mode
        other_bits = mode & 0o077
        if other_bits:
            perms = oct(stat.S_IMODE(mode))
            console.print(
                f"[bold yellow]WARNING:[/] Config file {path} has permissive "
                f"permissions ({perms}). It may contain API keys.\n"
                f"  Fix with: [cyan]chmod 600 {path}[/]"
            )
            return False
    except OSError:
        pass
    return True


def _load_config(path: Path = CONFIG_PATH) -> dict | None:
    """Load config.json with permission check."""
    if not path.exists():
        console.print("[red]Not initialized. Run: netclaw init[/]")
        return None
    _check_config_permissions(path)
    with open(path) as f:
        return json.load(f)


@click.group()
@click.option("--verbose", "-v", is_flag=True, help="Verbose logging")
def cli(verbose):
    """NetClaw — AI Battle Agent"""
    setup_logging(verbose)


@cli.command()
def init():
    """Initialize NetClaw workspace and config."""
    console.print(Panel(
        "[bold green]NetClaw Setup[/]",
        subtitle="AI Battle Agent",
    ))

    NETCLAW_DIR.mkdir(parents=True, exist_ok=True)
    os.chmod(NETCLAW_DIR, 0o700)
    (NETCLAW_DIR / "agents").mkdir(exist_ok=True)

    if not CONFIG_PATH.exists():
        fd = os.open(CONFIG_PATH, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(DEFAULT_CONFIG, f, indent=2)
        console.print(f"  Config created: {CONFIG_PATH}")
    else:
        console.print(f"  Config exists: {CONFIG_PATH}")

    console.print()
    console.print("[bold green]Setup complete![/]")
    console.print()
    console.print("Next steps:")
    console.print("  1. Set your LLM API key in config:")
    console.print(f"     [cyan]nano {CONFIG_PATH}[/]")
    console.print("  2. Join an arena:")
    console.print("     [cyan]netclaw agent join --arena https://arena.netclaw.io[/]")


@cli.group()
def agent():
    """Agent management commands."""
    pass


@agent.command("list")
def agent_list():
    """List configured agents."""
    config = _load_config()
    if config is None:
        return

    table = Table(title="Configured Agents")
    table.add_column("ID", style="cyan")
    table.add_column("Provider", style="green")
    table.add_column("Model")
    table.add_column("Categories")
    table.add_column("API Key")

    for a in config.get("agents", []):
        key = a.get("api_key", "")
        key_status = "set" if key else "missing"
        table.add_row(
            a["agent_id"],
            a["provider"],
            a.get("model", "default"),
            ", ".join(a.get("categories", ["all"])),
            key_status,
        )

    console.print(table)


@agent.command("add")
@click.argument("agent_id")
@click.option("--provider", "-p", default="groq", help="LLM provider")
@click.option("--model", "-m", default="", help="Model name")
@click.option("--wallet", "-w", default="", help="BNB wallet address (0x...)")
def agent_add(agent_id, provider, model, wallet):
    """Add a new agent."""
    if not _AGENT_ID_RE.match(agent_id):
        console.print("[red]Invalid agent ID: use 1-64 alphanumeric, dash, or underscore chars[/]")
        return
    config = _load_config()
    if config is None:
        return

    # Validate wallet if provided
    if wallet:
        if len(wallet) != 42 or not wallet.startswith("0x"):
            console.print("[red]Invalid wallet: must be 42 chars starting with 0x[/]")
            return
        try:
            int(wallet[2:], 16)
        except ValueError:
            console.print("[red]Invalid wallet: not a valid hex address[/]")
            return

    api_key = click.prompt("API key for provider", default="", hide_input=True, show_default=False)

    # Check duplicate
    for a in config["agents"]:
        if a["agent_id"] == agent_id:
            console.print(f"[red]Agent '{agent_id}' already exists[/]")
            return

    # Generate cryptographic agent secret (ownership proof)
    agent_secret = secrets.token_hex(32)

    new_agent = {
        "agent_id": agent_id,
        "provider": provider,
        "model": model or "default",
        "categories": ["text", "code", "reasoning", "creative", "knowledge"],
        "wallet": wallet,
        "agent_secret": agent_secret,
    }
    if api_key:
        new_agent["api_key"] = api_key
    if provider in ("local", "ollama", "llamacpp", "vllm", "lmstudio"):
        _local_defaults = {
            "ollama":   "http://localhost:11434/v1/chat/completions",
            "vllm":     "http://localhost:8000/v1/chat/completions",
            "llamacpp": "http://localhost:8080/v1/chat/completions",
            "lmstudio": "http://localhost:1234/v1/chat/completions",
            "local":    "http://localhost:8080/v1/chat/completions",
        }
        new_agent["base_url"] = _local_defaults.get(provider, _local_defaults["ollama"])

    config["agents"].append(new_agent)

    # Atomic write with restricted permissions to protect API keys
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, CONFIG_PATH)

    console.print(f"Agent '{agent_id}' added ({provider}/{model})")
    console.print(Panel(
        f"[bold yellow]AGENT SECRET KEY[/]\n\n"
        f"Hint: [cyan]{agent_secret[:8]}...{agent_secret[-8:]}[/]\n\n"
        f"Full key saved in config.json (protected 0600).\n"
        f"[bold red]Do NOT share your config.json — anyone with this key can act as your agent.[/]",
    ))


@agent.command("delete")
@click.argument("agent_id")
@click.option("--arena", "-a", default=None, help="Arena server URL (to delete server-side too)")
@click.confirmation_option(prompt="Are you sure you want to delete this agent?")
def agent_delete(agent_id, arena):
    """Delete an agent from config and optionally from the arena server."""
    config = _load_config()
    if config is None:
        return

    # Find agent in config
    target = None
    target_idx = None
    for i, a in enumerate(config["agents"]):
        if a["agent_id"] == agent_id:
            target = a
            target_idx = i
            break

    if target is None:
        console.print(f"[red]Agent '{agent_id}' not found in config[/]")
        return

    agent_secret = target.get("agent_secret", "")
    arena_url = arena or config.get("arena_url", "")
    arena_key = config.get("arena_key", "")

    # Try to delete from server
    server_deleted = False
    server_error = ""
    if arena_url and agent_secret:
        try:
            headers = {}
            if arena_key:
                headers["Authorization"] = f"Bearer {arena_key}"
            if agent_secret:
                headers["X-Agent-Secret"] = agent_secret
            r = httpx.post(
                f"{arena_url}/api/agents/delete",
                json={"agent_id": agent_id},
                headers=headers,
                timeout=10,
            )
            if r.status_code == 200:
                server_deleted = True
            elif r.status_code == 404:
                server_deleted = True  # Already gone
            elif r.status_code == 403:
                server_error = "Server rejected delete (403 Forbidden)"
                console.print(f"[bold red]WARNING: {server_error}[/]")
                console.print("Your local config still has the secret. Aborting to prevent data loss.")
                console.print("Fix the issue and retry, or delete locally with: netclaw agent delete {agent_id}")
                return
            elif r.status_code == 409:
                server_error = "Agent is participating in an active battle — try again later"
                console.print(f"[bold red]WARNING: {server_error}[/]")
                return
            else:
                server_error = f"Server returned {r.status_code}"
        except httpx.ConnectError:
            server_error = "Cannot reach server"
        except Exception as e:
            server_error = str(e)

    # Remove from config
    config["agents"].pop(target_idx)

    # Atomic write
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, CONFIG_PATH)

    msg = f"[bold red]Agent Deleted[/]\n\nAgent: {agent_id}\nConfig: [green]removed[/]"
    if arena_url:
        if server_deleted:
            msg += "\nServer: [green]removed[/]"
        elif server_error:
            msg += f"\nServer: [yellow]{server_error}[/]"
        else:
            msg += "\nServer: [yellow]not attempted (no secret or no arena URL)[/]"
    console.print(Panel(msg))


@agent.command("join")
@click.option("--arena", "-a", default=None, help="Arena server URL")
@click.option("--agent-id", "-n", default=None, help="Agent ID (uses first in config if not set)")
@click.option("--poll", "-p", default=10, type=int, help="Poll interval in seconds")
def agent_join(arena, agent_id, poll):
    """Join a remote Arena and compete automatically."""
    config = _load_config()
    if config is None:
        return

    arena_url = arena or config.get("arena_url", "https://arena.netclaw.io")

    # Find agent config
    agent_cfg = None
    if agent_id:
        for a in config["agents"]:
            if a["agent_id"] == agent_id:
                agent_cfg = a
                break
        if not agent_cfg:
            console.print(f"[red]Agent '{agent_id}' not found in config[/]")
            return
    else:
        if not config["agents"]:
            console.print("[red]No agents configured. Run: netclaw agent add[/]")
            return
        agent_cfg = config["agents"][0]

    console.print(Panel(
        f"[bold green]Joining Arena[/]\n"
        f"Arena: {arena_url}\n"
        f"Agent: {agent_cfg['agent_id']} ({agent_cfg['provider']}/{agent_cfg.get('model', 'default')})\n"
        f"Poll: every {poll}s",
    ))

    asyncio.run(_join_arena(agent_cfg, arena_url, poll, arena_key=config.get("arena_key", "")))


def _is_netclaw_process(pid: int) -> bool:
    """Check if PID belongs to a netclaw process (not a random unrelated process)."""
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True, text=True, timeout=5,
        )
        return "netclaw" in result.stdout.lower()
    except Exception:
        return False


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with given PID is alive."""
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _write_pid_file(agent_id: str) -> Path:
    """Write PID file for an agent. Raises if agent is already running."""
    if not _AGENT_ID_RE.match(agent_id):
        raise RuntimeError(f"Invalid agent_id: must be 1-64 alphanumeric/dash/underscore chars")
    PIDS_DIR.mkdir(parents=True, exist_ok=True)
    pid_file = PIDS_DIR / f"{agent_id}.pid"

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text().strip())
            if _is_pid_alive(existing_pid) and _is_netclaw_process(existing_pid):
                raise RuntimeError(
                    f"Agent '{agent_id}' already running (PID {existing_pid})"
                )
        except (ValueError, OSError):
            pass
        pid_file.unlink(missing_ok=True)

    try:
        fd = os.open(pid_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
    except FileExistsError:
        raise RuntimeError(f"Agent '{agent_id}' PID file created by another process (race)")
    return pid_file


def _remove_pid_file(agent_id: str):
    """Remove PID file for an agent."""
    pid_file = PIDS_DIR / f"{agent_id}.pid"
    pid_file.unlink(missing_ok=True)


async def _join_arena(agent_cfg: dict, arena_url: str, poll_interval: int, arena_key: str = ""):
    """Async arena join."""
    from netclaw.providers.router import ProviderRouter, PROVIDER_MAP
    from netclaw.providers.groq_provider import GroqProvider
    from netclaw.core.agent import NetClawAgent
    from netclaw.arena.client import ArenaClient

    provider_name = agent_cfg["provider"]
    provider_cls = PROVIDER_MAP.get(provider_name, GroqProvider)

    kwargs = {"api_key": agent_cfg.get("api_key", "not-needed"), "model": agent_cfg.get("model", "")}
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
        arena_url=arena_url,
        poll_interval=poll_interval,
        wallet=agent_cfg.get("wallet", ""),
        arena_key=arena_key,
        agent_secret=agent_cfg.get("agent_secret", ""),
    )

    aid = agent_cfg["agent_id"]
    try:
        _write_pid_file(aid)
    except RuntimeError as e:
        console.print(f"[red]{e}[/]")
        return

    try:
        await client.start()
    except KeyboardInterrupt:
        await client.stop()
    finally:
        _remove_pid_file(aid)


@cli.command()
@click.option("--agent-id", "-n", default=None, help="Stop a specific agent (default: all)")
def close(agent_id):
    """Stop running NetClaw agents."""
    if agent_id and not _AGENT_ID_RE.match(agent_id):
        console.print("[red]Invalid agent ID format[/]")
        return
    if not PIDS_DIR.exists():
        console.print(Panel("[dim]No active agents found.[/]", title="NetClaw Close"))
        return

    if agent_id:
        pid_files = list(PIDS_DIR.glob(f"{agent_id}.pid"))
        if not pid_files:
            console.print(Panel(
                f"[dim]Agent '{agent_id}' is not running.[/]",
                title="NetClaw Close",
            ))
            return
    else:
        pid_files = sorted(PIDS_DIR.glob("*.pid"))
        if not pid_files:
            console.print(Panel("[dim]No active agents found.[/]", title="NetClaw Close"))
            return

    results = []
    stopped = 0
    already_gone = 0

    for pid_file in pid_files:
        name = pid_file.stem
        try:
            pid = int(pid_file.read_text().strip())
        except (ValueError, OSError):
            pid_file.unlink(missing_ok=True)
            results.append(f"{name}: [dim]stale PID file removed[/]")
            already_gone += 1
            continue

        if not _is_pid_alive(pid):
            pid_file.unlink(missing_ok=True)
            results.append(f"{name}: [dim]already stopped[/]")
            already_gone += 1
            continue

        if not _is_netclaw_process(pid):
            pid_file.unlink(missing_ok=True)
            results.append(f"{name}: [dim]stale PID (process is not netclaw)[/]")
            already_gone += 1
            continue

        # Graceful shutdown: SIGINT (like CTRL+C)
        os.kill(pid, signal.SIGINT)

        # Poll for exit (max 5 seconds)
        dead = False
        for _ in range(17):  # 17 * 0.3 = ~5.1s
            time.sleep(0.3)
            if not _is_pid_alive(pid):
                dead = True
                break

        if not dead:
            # Force kill
            try:
                os.kill(pid, signal.SIGKILL)
            except OSError:
                pass

        pid_file.unlink(missing_ok=True)
        results.append(f"{name} (PID {pid}): [green]stopped[/]")
        stopped += 1

    # Summary
    lines = "\n".join(results)
    summary = []
    if stopped:
        summary.append(f"{stopped} agent{'s' if stopped != 1 else ''} stopped")
    if already_gone:
        summary.append(f"{already_gone} already gone")
    footer = ", ".join(summary) if summary else ""

    content = f"{lines}\n\n[bold]{footer}[/]" if footer else lines
    console.print(Panel(content, title="NetClaw Close"))


@cli.command()
@click.option("--arena", "-a", default=None, help="Arena server URL")
def status(arena):
    """Show arena status."""
    config = _load_config()
    if config is None:
        return

    arena_url = arena or config.get("arena_url", "https://arena.netclaw.io")

    try:
        r = httpx.get(f"{arena_url}/api/status", timeout=15)
        data = r.json()

        table = Table(title="NetClaw Arena Status")
        table.add_column("Metric", style="cyan")
        table.add_column("Value", style="green")

        table.add_row("Total Battles", str(data.get("total_battles", 0)))
        table.add_row("Resolved", str(data.get("resolved_battles", 0)))
        table.add_row("Agents", str(data.get("registered_agents", 0)))
        table.add_row(
            "Reward Pool",
            f"{data.get('reward_pool_remaining', 0):,.0f} $CLAW"
        )
        table.add_row(
            "Distributed",
            f"{data.get('reward_pool_distributed', 0):,.0f} $CLAW"
        )

        if data.get("active_battle"):
            ab = data["active_battle"]
            table.add_row(
                "Active Battle",
                f"{ab['battle_id']} | {ab['phase']} | {ab['category']}"
            )

        console.print(table)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Arena returned error {e.response.status_code}[/]")
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to arena at {arena_url}[/]")
    except Exception:
        console.print(f"[red]Error connecting to arena[/]")


@cli.command()
@click.option("--arena", "-a", default=None, help="Arena server URL")
def leaderboard(arena):
    """Show the agent leaderboard."""
    config = _load_config()
    if config is None:
        return

    arena_url = arena or config.get("arena_url", "https://arena.netclaw.io")

    try:
        r = httpx.get(f"{arena_url}/api/leaderboard?top=20", timeout=15)
        data = r.json()

        table = Table(title="NetClaw Leaderboard")
        table.add_column("#", style="bold")
        table.add_column("Agent", style="cyan")
        table.add_column("$CLAW", style="green")
        table.add_column("Wins", style="yellow")
        table.add_column("Battles")
        table.add_column("Win Rate")
        table.add_column("Reputation")
        table.add_column("Avg Score")

        for i, entry in enumerate(data):
            rank = str(i + 1)
            battles = entry.get("battles", 0)
            wins = entry.get("wins", 0)
            win_rate = f"{wins/battles*100:.0f}%" if battles > 0 else "-"

            table.add_row(
                rank,
                entry.get("agent_id", "?"),
                f"{entry.get('total_claw', 0):,.1f}",
                str(wins),
                str(battles),
                win_rate,
                f"{entry.get('reputation', 50):.1f}",
                f"{entry.get('avg_score', 0):.1f}",
            )

        console.print(table)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Arena returned error {e.response.status_code}[/]")
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to arena at {arena_url}[/]")
    except Exception:
        console.print(f"[red]Error connecting to arena[/]")


def _validate_wallet(address: str) -> bool:
    """Validate BNB wallet address format."""
    if len(address) != 42 or not address.startswith("0x"):
        return False
    try:
        int(address[2:], 16)
    except ValueError:
        return False
    return True


@cli.group()
def wallet():
    """Wallet management commands."""
    pass


@wallet.command("set")
@click.argument("address")
@click.option("--agent-id", "-n", default="", help="Agent ID (default: first in config)")
def wallet_set(address, agent_id):
    """Set BNB wallet address for an agent."""
    if not _validate_wallet(address):
        console.print("[red]Invalid wallet address. Must be 42 chars, 0x prefix, valid hex.[/]")
        return

    config = _load_config()
    if config is None:
        return

    if not config.get("agents"):
        console.print("[red]No agents configured. Run: netclaw agent add[/]")
        return

    # Find target agent
    target = None
    for a in config["agents"]:
        if agent_id and a["agent_id"] == agent_id:
            target = a
            break
        elif not agent_id:
            target = a
            break

    if not target:
        console.print(f"[red]Agent '{agent_id}' not found in config[/]")
        return

    target["wallet"] = address

    # Atomic write with restricted permissions to protect API keys
    tmp_path = CONFIG_PATH.with_suffix(".tmp")
    fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f, indent=2)
    os.replace(tmp_path, CONFIG_PATH)

    # Try to update server-side if arena is configured
    arena_url = config.get("arena_url", "")
    arena_key = config.get("arena_key", "")
    agent_secret = target.get("agent_secret", "")
    server_updated = False
    if arena_url and (arena_key or agent_secret):
        try:
            headers = {}
            if arena_key:
                headers["Authorization"] = f"Bearer {arena_key}"
            if agent_secret:
                headers["X-Agent-Secret"] = agent_secret
            r = httpx.post(
                f"{arena_url}/api/agents/wallet",
                json={"agent_id": target["agent_id"], "wallet": address},
                headers=headers,
                timeout=10,
            )
            if r.status_code == 200:
                server_updated = True
        except Exception:
            pass

    truncated = f"{address[:8]}...{address[-6:]}"
    msg = f"[bold green]Wallet Set[/]\n\nAgent: {target['agent_id']}\nWallet: {truncated}"
    if server_updated:
        msg += "\nServer: [green]updated[/]"
    elif arena_url and arena_key:
        msg += "\nServer: [yellow]failed to update (will sync on next join)[/]"
    console.print(Panel(msg))


@wallet.command("show")
@click.option("--agent-id", "-n", default="", help="Agent ID (default: first in config)")
def wallet_show(agent_id):
    """Show wallet status for an agent."""
    config = _load_config()
    if config is None:
        return

    if not config.get("agents"):
        console.print("[red]No agents configured. Run: netclaw agent add[/]")
        return

    # Find target agent(s)
    agents_to_show = []
    if agent_id:
        for a in config["agents"]:
            if a["agent_id"] == agent_id:
                agents_to_show.append(a)
                break
        if not agents_to_show:
            console.print(f"[red]Agent '{agent_id}' not found in config[/]")
            return
    else:
        agents_to_show = config["agents"]

    arena_url = config.get("arena_url", "")

    table = Table(title="Wallet Status")
    table.add_column("Agent", style="cyan")
    table.add_column("Local Wallet")
    table.add_column("Server Wallet")
    table.add_column("Match")

    for a in agents_to_show:
        local_wallet = a.get("wallet", "")
        local_display = f"{local_wallet[:8]}...{local_wallet[-6:]}" if local_wallet else "[red]not set[/]"

        server_wallet = ""
        if arena_url:
            try:
                r = httpx.get(f"{arena_url}/api/agents/{a['agent_id']}", timeout=10)
                if r.status_code == 200:
                    server_wallet = r.json().get("wallet", "")
            except Exception:
                pass

        server_display = f"{server_wallet[:8]}...{server_wallet[-6:]}" if server_wallet else "[dim]n/a[/]"
        if local_wallet and server_wallet:
            match = "[green]yes[/]" if local_wallet.lower() == server_wallet.lower() else "[red]no[/]"
        else:
            match = "[dim]-[/]"

        table.add_row(a["agent_id"], local_display, server_display, match)

    console.print(table)


@cli.command()
@click.option("--arena", "-a", default=None, help="Arena server URL")
@click.option("--agent-id", "-n", default=None, help="Agent ID (default: first in config)")
def rewards(arena, agent_id):
    """Show your $CLAW rewards (earned, paid, pending)."""
    config = _load_config()
    if config is None:
        return

    arena_url = arena or config.get("arena_url", "https://arena.netclaw.io")

    # Resolve agent_id
    if not agent_id:
        if not config.get("agents"):
            console.print("[red]No agents configured. Run: netclaw agent add[/]")
            return
        agent_id = config["agents"][0]["agent_id"]

    try:
        r = httpx.get(f"{arena_url}/api/rewards/{agent_id}", timeout=15)

        if r.status_code == 503:
            console.print("[yellow]Reward tracker not active on this arena.[/]")
            return
        if r.status_code == 400:
            console.print(f"[red]Agent '{agent_id}' not found on arena.[/]")
            return

        r.raise_for_status()
        data = r.json()

        table = Table(title=f"Rewards — {agent_id}")
        table.add_column("Metric", style="cyan")
        table.add_column("$CLAW", style="green", justify="right")

        table.add_row("Earned", f"{data.get('earned', 0):,.4f}")
        table.add_row("Paid", f"{data.get('paid', 0):,.4f}")
        table.add_row("[bold]Pending[/]", f"[bold]{data.get('pending', 0):,.4f}[/]")

        console.print(table)
    except httpx.HTTPStatusError as e:
        console.print(f"[red]Arena returned error {e.response.status_code}[/]")
    except httpx.ConnectError:
        console.print(f"[red]Cannot connect to arena at {arena_url}[/]")
    except Exception:
        console.print(f"[red]Error connecting to arena[/]")


if __name__ == "__main__":
    cli()
