"""Query helpers.

This module documents every query in detail so that a reader can follow
the data flow without opening the database schema. Each function below
returns a SQL string; nothing here talks to the database directly.
"""


def active_users_sql():
    """Return SQL selecting active users."""
    return """
        SELECT id, email
        FROM users
        WHERE active = 1
        ORDER BY id
    """
