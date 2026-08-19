"""Lightfield API client — agent-native CRM (accounts, contacts, opportunities)."""

from .client import LightfieldClient, scope_granted

__all__ = ["LightfieldClient", "scope_granted"]
