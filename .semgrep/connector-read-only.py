# Semgrep self-test fixture for connector-read-only.yaml (spec 18.3: "Semgrep rule self-test").
# `semgrep --test .semgrep` checks that every `ruleid:` line matches and every `ok:` line does not.
# This file is excluded from ruff, mypy, bandit and the real Semgrep scan.


async def sftp(client, path):
    # ok: carto-connector-read-only
    await client.listdir(path)
    # ok: carto-connector-read-only
    await client.stat(path)
    # ruleid: carto-connector-read-only
    await client.put("local.csv", path)
    # ruleid: carto-connector-read-only
    await client.remove(path)
    # ruleid: carto-connector-read-only
    await client.rename(path, path + ".done")
    # ruleid: carto-connector-read-only
    await client.mkdir(path)


def http(session, url):
    # ok: carto-connector-read-only
    session.get(url)
    # ok: carto-connector-read-only
    session.head(url)
    # ok: carto-connector-read-only
    session.request("GET", url)
    # ruleid: carto-connector-read-only
    session.post(url, data={})
    # ruleid: carto-connector-read-only
    session.delete(url)
    # ruleid: carto-connector-read-only
    session.request("PUT", url)


def sql(cursor):
    # ok: carto-connector-read-only
    cursor.execute("SELECT id FROM purchase_orders WHERE updated_at > :watermark")
    # ok: carto-connector-read-only
    cursor.execute("WITH recent AS (SELECT 1) SELECT * FROM recent")
    # ruleid: carto-connector-read-only
    cursor.execute("UPDATE purchase_orders SET status = 'SHIPPED'")
    # ruleid: carto-connector-read-only
    cursor.execute("delete from purchase_orders")
    # ruleid: carto-connector-read-only
    cursor.executemany("INSERT INTO t VALUES (?)", [(1,)])
