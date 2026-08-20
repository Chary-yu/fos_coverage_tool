"""Small DB-API transaction boundary shared by VNext services.

Repositories deliberately never commit. A service owns one transaction and
passes its connection to all repositories participating in the operation.
The implementation is limited to Python 3.6-compatible standard-library
APIs so it can run against SQLite test databases and MariaDB 5.5 via PyMySQL.
"""

from contextlib import contextmanager


class TransactionManager(object):
    """Own a connection or a connection factory and commit exactly once."""

    def __init__(self, connection=None, connection_factory=None):
        if connection is None and connection_factory is None:
            raise ValueError("connection or connection_factory is required")
        self.connection = connection
        self.connection_factory = connection_factory

    @contextmanager
    def transaction(self):
        owned = self.connection is None
        connection = self.connection_factory() if owned else self.connection
        if connection is None:
            raise RuntimeError("database connection is unavailable")
        try:
            yield connection
            connection.commit()
        except Exception:
            try:
                connection.rollback()
            except Exception:
                pass
            raise
        finally:
            if owned:
                close = getattr(connection, "close", None)
                if close:
                    close()


@contextmanager
def transaction(connection):
    """Convenience wrapper used by small services and tests."""
    with TransactionManager(connection=connection).transaction() as conn:
        yield conn
