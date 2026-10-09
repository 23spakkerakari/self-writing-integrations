"""Database access for core (spec 7.2, 7.3, 14.4, 14.7).

:mod:`carto_core.db.clickhouse` builds the client from settings and writes canonical events into
``events`` and ``event_identifiers``; :mod:`carto_core.db.postgres` builds the SQLAlchemy engine
for the metadata store. Both read passwords from files only and never put a DSN with a password
in a log line or an exception. Every statement outside the migration files is parameterized
(spec 14.7).
"""
