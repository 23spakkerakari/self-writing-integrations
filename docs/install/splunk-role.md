# Splunk: a search-only role for carto (spec 8.1.3)

carto pulls events with the REST search export endpoint (`/services/search/jobs/export`) using
an authentication token for a dedicated user. The user's role may search only the indexes you
list, and nothing else.

1. **Create a role** `carto_search` (Settings > Roles > New role):
   - Inherit from nothing (do not inherit `user`).
   - Capabilities: `search`, `rest_properties_get` (to read its own context). Do **not** grant
     `edit_*`, `delete_*`, `admin_*`, `change_*`, `restart_*`, `output_file` or `rtsearch`.
   - Indexes searched by default and allowed: only the indexes carto needs (for example `orders`).
   - Search job limits: concurrent searches 2 (carto's `max_concurrency` default), disk quota as
     your policy requires.
2. **Create a user** `carto` with that role only.
3. **Create an authentication token** (Settings > Tokens) for the user with an expiry that matches
   your rotation policy, and store it in your secret manager; reference it from `sources.yaml`
   as `secret_ref`, never inline.
4. **Test** from carto (Setup > Sources > Test). The test runs a one-minute search with
   `| head 1`, reads the user's roles and capabilities, refuses tokens whose roles carry a
   write capability, and lists the indexes the token can see.
