"""
Shared database utility functions for the Photonarium image database.

This leaf module contains pure-Python helpers used across database modules.
It imports only the standard library so it can never introduce circular-import
issues.
"""

from __future__ import annotations


def sql_placeholders(items) -> str:
    """Return comma-separated ``?`` placeholders for use in SQL IN clauses.

    Args:
        items: Any sized iterable whose ``len()`` determines the placeholder
            count.

    Returns:
        A string like ``'?,?,?'`` with one ``?`` per item.
    """
    return ','.join('?' * len(items))
