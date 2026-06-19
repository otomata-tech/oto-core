"""`oto google chat` command surface."""

import json

import typer
from typing import Optional

app = typer.Typer(help="Google Chat tools (spaces, send, dm, history)")


@app.command("spaces")
def chat_spaces(
    dm: bool = typer.Option(False, "--dm", help="Only direct-message spaces"),
    limit: int = typer.Option(100, "--limit", "-n", help="Max spaces"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Google account name"),
):
    """List Google Chat spaces (rooms + DMs) the account belongs to."""
    from oto.tools.google.chat.lib.chat_client import ChatClient

    client = ChatClient(account=account)
    filter_ = 'spaceType = "DIRECT_MESSAGE"' if dm else None
    spaces = client.list_spaces(filter_=filter_, max_results=limit)
    print(json.dumps({"count": len(spaces), "spaces": spaces}, indent=2, ensure_ascii=False))


@app.command("send")
def chat_send(
    text: str = typer.Option(..., "--text", "-t", help="Message text (Chat formatting: *bold*, _italic_)"),
    to: Optional[str] = typer.Option(None, "--to", help="Recipient email — sends a direct message (resolves the DM space)"),
    space: Optional[str] = typer.Option(None, "--space", "-s", help="Target space resource name (e.g. spaces/AAAA)"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Google account name"),
):
    """Send a Google Chat message — either to a person (--to) or into a space (--space)."""
    from oto.tools.google.chat.lib.chat_client import ChatClient

    if bool(to) == bool(space):
        raise typer.BadParameter("Pass exactly one of --to (direct message) or --space.")

    client = ChatClient(account=account)
    if to:
        result = client.send_dm(to, text)
    else:
        result = client.send(space, text)
    print(json.dumps(result, indent=2, ensure_ascii=False))


@app.command("dm-resolve")
def chat_dm_resolve(
    user: str = typer.Argument(..., help="Recipient email or user id"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Google account name"),
):
    """Resolve the direct-message space name with a user."""
    from oto.tools.google.chat.lib.chat_client import ChatClient

    client = ChatClient(account=account)
    space = client.find_dm(user)
    print(json.dumps({"user": user, "space": space}, indent=2, ensure_ascii=False))


@app.command("history")
def chat_history(
    space: str = typer.Argument(..., help="Space resource name (e.g. spaces/AAAA)"),
    limit: int = typer.Option(20, "--limit", "-n", help="Max messages"),
    account: Optional[str] = typer.Option(None, "--account", "-a", help="Google account name"),
):
    """List recent messages in a Google Chat space (most recent first)."""
    from oto.tools.google.chat.lib.chat_client import ChatClient

    client = ChatClient(account=account)
    messages = client.list_messages(space, max_results=limit)
    print(json.dumps({"count": len(messages), "messages": messages}, indent=2, ensure_ascii=False))
