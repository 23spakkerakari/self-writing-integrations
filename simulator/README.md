# carto simulator

Deterministic multi-system synthetic data with ground truth (spec Section 19). M0 ships scenario A
("shop") in batch mode: native-format files per system plus `ground_truth/` for the eval harness
(Section 18.4). Nothing here is real customer data (spec 0.1 item 7).

## Run it

```sh
make sim SCENARIO=shop DAYS=14 SEED=1            # writes sim-out/shop
uv run carto-sim generate --scenario shop --days 14 --seed 1 --daily-volume 800 \
    --start-date 2026-09-23 --out sim-out/shop [--no-faults] [--noise-rate 1.0] [--pii-density 0.3]
uv run carto-sim list
```

`carto-sim generate` prints a short summary and returns 0; an unknown scenario or an invalid
request prints a message on stderr and returns 2. The defaults of `GenerationRequest`
(`carto_simulator.api`) are the CLI defaults. From Python:

```python
from pathlib import Path
from carto_simulator.api import GenerationRequest, generate

result = generate(GenerationRequest(days=3, daily_volume=40), Path("out"))
result.manifest.counts  # per source, plus transactions, events, noise, files
```

The output directory is created when missing. An existing directory is emptied first, so stale
files never remain, but only when it is empty or holds a previous run
(`ground_truth/manifest.json`); any other populated directory, or a path that is a file, is
refused (`ValueError` from `generate`, exit 2 from the CLI) so a mistyped `--out` never wipes a
checkout.
The default run (14 days, 800 orders per weekday, about 115,000 records, 51 MB) takes 6 to 11 s
on a laptop and well under 1 GB of memory in pure Python.

## Output layout

```
out/
  README.md                          generated: sources, counts, faults, determinism note
  webstore/app-YYYY-MM-DD.ndjson     one file per UTC day
  orders/order-svc-YYYY-MM-DD.log    logfmt, one file per UTC day
  payments/messages-YYYY-MM-DD.xml   one XML document per line, one file per local day
  warehouse/purchase_orders.sql      CREATE TABLE + INSERTs (+ ALTER TABLE ... RENAME COLUMN)
  warehouse/purchase_orders.csv      rows created before the rename (header uses po_num)
  warehouse/purchase_orders.renamed.csv   rows from the rename day on (po_number); faults on only
  warehouse/export-job-YYYY-MM-DD.log     unstructured text, one file per local day
  shipping/outbound/SHIP_YYYYMMDD_HHMM.csv   nightly file drops; mtime = true arrival instant (UTC)
  shipping/shipping-app-YYYY-MM-DD.log       logfmt, one file per UTC day
  ground_truth/                      see "Ground truth" below
```

Local time is America/New_York (`zoneinfo`, DST handled). Timestamps inside the data are UTC
except the two warehouse sources, which use naive local time and run 90 s fast.

## Sources

| Source | System | Kind | Format | Timezone | Skew | Identifiers |
| --- | --- | --- | --- | --- | --- | --- |
| `src_webstore_log` | `sys_webstore` Webstore | log file | NDJSON | UTC | 0 | `cart_id` (`c-88213`) |
| `src_orders_log` | `sys_orders` Order system | log file | logfmt | UTC | 0 | `order_id` (`4471`), `cart_id`, `merchant_ref` (`X9-0442`) |
| `src_payments_xml` | `sys_payments` Payments | log file | XML lines | America/New_York (offset in data) | 0 | `merchantRef` |
| `src_wms_db` | `sys_warehouse` Warehouse | SQL table | SQL seed + CSV | America/New_York (naive) | +90 s | `po_num` (`88-210`), `order_ref` (`SO-0004471`) |
| `src_wms_export_log` | `sys_warehouse` Warehouse | log file | text | America/New_York (naive) | +90 s | file names |
| `src_ship_sftp` | `sys_shipping` Shipping | file drop | files | UTC (mtime) | 0 | file name (batch key) |
| `src_ship_log` | `sys_shipping` Shipping | log file | logfmt | UTC | 0 | `shipment_no` (`SH-5521`), `po_num`, `file`, `manifest_id` |

## Formats, one example line each

```
webstore   {"ts": "2026-09-23T13:04:06.001Z", "level": "info", "msg": "cart created", "cart_id": "c-88213", "items": 3, "region": "us-east", "channel": "web"}
webstore   {"ts": "2026-09-23T13:16:40.381Z", "level": "info", "msg": "checkout completed", "cart_id": "c-88213", "total": 129.99, "payment_method": "card", "customer_name": "Jane Smith", "customer_email": "jane.smith+mk3f9a2c1e@example.com"}
orders     ts=2026-09-23T13:16:46.566Z level=info msg="order created from cart" order_id=4471 cart_id=c-88213 total=129.99 channel=web
orders     ts=2026-09-23T13:16:56.709Z level=info msg="payment requested" order_id=4471 merchant_ref=X9-0442 amount=129.99 currency=USD
orders     ts=2026-09-28T14:20:19.579Z level=error msg="payment request failed" order_id=7816 merchant_ref=X9-3787 http_status=503
orders     ts=2026-09-23T13:17:04.213Z level=info msg="order released" order_id=4471 status=RELEASED
payments   <paymentMessage><timestamp>2026-09-23T09:17:01.292-04:00</timestamp><merchantRef>X9-0442</merchantRef><amount>129.99</amount><currency>USD</currency><cardholderName>Jane Smith</cardholderName><status>AUTHORIZED</status><httpStatus>200</httpStatus><processor>cardnet</processor></paymentMessage>
payments   <paymentMessage><timestamp>2026-09-28T10:20:19.186-04:00</timestamp><merchantRef>X9-3787</merchantRef><amount>596.99</amount><currency>USD</currency><status>ERROR</status><httpStatus>503</httpStatus><error>Service Unavailable</error><processor>cardnet</processor></paymentMessage>
payments   <heartbeat><timestamp>2026-09-23T00:23:06.570-04:00</timestamp><status>OK</status></heartbeat>
warehouse  INSERT INTO purchase_orders (id, po_num, order_ref, status, warehouse_code, order_total, order_date, customer_name, ship_to_address, created_by, created_at, updated_at) VALUES (1, '88-210', 'SO-0004471', 'SHIPPED', 'DC-01', 129.99, '2026-09-23', 'Jane Smith', '282 mk5349da48 Street, Omaha, NE 52733', 'svc_wms_integration', '2026-09-23 09:21:04', '2026-09-23 21:41:31');
warehouse  1,88-210,SO-0004471,SHIPPED,DC-01,129.99,2026-09-23,Jane Smith,"282 mk5349da48 Street, Omaha, NE 52733",svc_wms_integration,2026-09-23 09:21:04,2026-09-23 21:41:31
export log 2026-09-23 21:12:41 INFO PO export finished: 412 POs written to SHIP_20260923_2112.csv
export log 2026-09-23 21:13:36 INFO SFTP upload complete: SHIP_20260923_2112.csv (23891 bytes)
export log 2026-10-01 21:14:30 ERROR SFTP upload failed: Permission denied (/outbound/shipping/SHIP_20261001_2113.csv)
export log 2026-09-23 10:00:12 INFO export scheduler idle, next run 21:10
SHIP file  po_num,order_ref,warehouse_code,carrier,service_level,weight_kg  /  88-210,SO-0004471,DC-01,UPS,GROUND,15.38
shipping   ts=2026-09-24T01:20:39.439Z level=info msg="shipment created" shipment_no=SH-5521 po_num=88-210 file=SHIP_20260923_2112.csv carrier=UPS service=GROUND
shipping   ts=2026-09-24T02:13:52.000Z level=info msg="carrier pickup" shipment_no=SH-5521 manifest_id=MAN-20260923-01 carrier=UPS
shipping   ts=2026-09-24T00:05:53.606Z level=info msg="carrier rate table refreshed" carrier=USPS rows=412
```

The SQL seed is standard SQL that both sqlite3 and PostgreSQL execute: strings with doubled single
quotes, no backslash escapes, timestamps as `'YYYY-MM-DD HH:MM:SS'`, `order_total` as a decimal
literal. XML text is escaped with `xml.sax.saxutils.escape`. logfmt values with spaces, quotes,
`=` or backslashes are double-quoted with `\"` and `\\` escapes.

## Lifecycle and timings

One transaction is one order, `txn_000001` ascending by cart creation time. Volumes:
`daily_volume` orders per weekday and `ceil(daily_volume / 2)` on Saturday and Sunday (the
expectation is used exactly, not drawn); hour-of-day weights in local time are low overnight, rise
from 07:00 and are highest from 11:00 to 21:00; the time within the hour is uniform.

| Step | Node | When | Line |
| --- | --- | --- | --- |
| 1 | `webstore:cart_created` | t0 | `cart created`, `cart_id` = `c-` + counter from 88213 |
| 2 | `webstore:checkout_completed` | t0 + U(2, 20) min | `checkout completed`; `customer_name` and `customer_email` with probability `pii_density` |
| 3 | `orders:order_created` | + U(5, 60) s | `order created from cart`, `order_id` from 4471 (bridge `cart_id` ~ `order_id`) |
| 4 | `orders:payment_requested` | + U(5, 30) s | `payment requested`, `merchant_ref` = `X9-` + counter from 442, at least 4 digits (bridge) |
| 5 | `payments:authorized` | + U(1, 15) s | `AUTHORIZED` message; `cardholderName` (same name as the checkout) with probability `pii_density` |
| 6 | `orders:order_released` | + U(1, 10) s | `order released` |
| 7 | `warehouse:po_created` | automated (70%): authorized + U(1, 5) min, `created_by=svc_wms_integration`, actor service. Clerk (30%): the first instant at or after authorized + 20 min inside Mon to Fri 08:00 to 18:00, plus U(0, 240) min; past 18:00 it moves to the next business day 08:00 + U(0, 60) min; `created_by` one of 8 clerks, actor human | one `purchase_orders` row; `order_ref` = `SO-` + order id zero-padded to 7 digits; 2% of clerk rows get a typo in `order_ref` (one digit replaced or two adjacent differing digits swapped), so the `digits.0` form no longer equals the order id |
| 8 | picking | created + U(30, 240) min inside 06:00 to 22:00; otherwise the next opening at 06:00 + U(0, 60) min (same day when before opening) | status `PICKED`; POs picked before 21:00 ride that day's export, later ones the next day's |
| 8 | `warehouse:export_finished`, `warehouse:upload_complete` | 21:10 + U(0, 5) min local on every day with at least one due PO; upload 30 to 60 s later | `PO export finished: N POs written to SHIP_YYYYMMDD_HHMM.csv`; HHMM is the warehouse's own clock (true + 90 s) |
| 9 | `shipping:file_arrived` | export + U(3, 8) min | the SHIP file lands; mtime = arrival; `batch_<file name without .csv>` |
| 10 | `shipping:shipment_created` | arrival + U(5, 70) min per PO | `shipment_no` = `SH-` + counter from 5521 in creation order; row status `SHIPPED` |
| 11 | `shipping:carrier_pickup` | 22:00 + U(0, 30) min local per day | one line per shipment created before the pickup time; later shipments ride the next day's manifest `MAN-YYYYMMDD-01` |

Row ids and PO numbers (`88-210` from a counter starting at 88210, three-digit prefix past 99999)
follow `created_at` order. `order_date` is the local date of `orders:order_created`. Rows carry
the final status (`CREATED`, `PICKED`, `SHIPPED`) and `updated_at` of the last change inside the
window. Steps falling after the window (last day 23:59:59 local) are not emitted and ground truth
records only what was emitted; a row whose later steps fall outside the window keeps the status it
had at the window end.

Noise (node `<system>:noise`, no transaction, no identifiers) has a Poisson count per hour with
mean `rate * noise_rate`, spread uniformly within the hour: webstore 40/h (`health check ok`,
`cache refresh`), orders 30/h (`scheduler tick`, `db pool stats`), payments 6/h (heartbeat),
warehouse export log 1/h (`export scheduler idle, next run 21:10`), shipping log 10/h
(`carrier rate table refreshed`). Lifecycle and noise lines are interleaved in each file in
rendered-time order (ties keep generation order).

## Faults

Days are 1-based from `start_date`; with the default start (Wednesday 2026-09-23) the fault days
are the weekdays the spec names. A fault whose day is beyond `days` is skipped. `--no-faults`
turns all of them off except the clock skew, which is a property of the warehouse source.

| Id | Kind | When (local) | What happens | Expected detection |
| --- | --- | --- | --- | --- |
| `f1_payments_503_spike` | `error_spike` | day 6, 10:20 to 11:00 | every payment attempt in the window gets `<status>ERROR</status><httpStatus>503</httpStatus><error>Service Unavailable</error>` (node `payments:error_503`, `is_error`) and the order log writes `level=error msg="payment request failed" ... http_status=503` (`orders:payment_failed`); the order retries every U(5, 10) min with the same `merchant_ref` until the first attempt after 11:00 | alert kind `error_rate`, cause `error_spike`, evidence `Service Unavailable` |
| `f2_missing_nightly_file` | `missing_file` | day 9, expected by 21:30 | the export runs (the fault needs a due PO that night; a day 9 without an export produces no FaultTruth), then exactly 12 `ERROR SFTP upload failed: Permission denied (/outbound/shipping/SHIP_...csv)` lines between 21:13 and 21:19 true time (`warehouse:upload_failed`); no SHIP file lands; day 10's file lists both days' POs and their shipments follow its arrival | alert kind `schedule`, cause `error_spike`, evidence `SFTP upload failed: Permission denied`; `end` = day 10's arrival |
| `f3_webstore_outage` | `visibility_gap` | day 10, 14:00 to 16:00 | webstore lines in the window are not written (no EventTruth either); orders and everything downstream continue | alert kind `freshness`, cause `visibility_gap`; must never be reported as a stall (spec 10.2) |
| `f4_schema_rename` | `schema_drift` | from day 12 00:00 | rows created from then on use column `po_number`: `ALTER TABLE ... RENAME COLUMN` between the inserts in the SQL seed, and the CSV export is split into `purchase_orders.csv` / `purchase_orders.renamed.csv` | alert kind `schema_drift`, attributes `{"field": "po_num", "renamed_to": "po_number"}` |
| `f5_partial_stall_dc03` | `partial_stall` | from day 13 10:00 | POs with `warehouse_code=DC-03` created from then on are never picked, exported or shipped (status stays `CREATED`) | alert kind `hop_deadline`, cause `partial_attribute_lift` ("what's different: warehouse_code=DC-03") |
| `f6_warehouse_clock_skew` | `clock_skew` | whole run | both warehouse sources render true time + 90 s (always on; the FaultTruth exists only with faults on) | no alert (`expected_alert` false) |

## Ground truth

`ground_truth/` holds the files described in `carto_simulator/ground_truth.py` (ADR 0006):

- `event_txn.ndjson`: one `EventTruth` per emitted record, streamed in `(observed_at, key)`
  order. `observed_at` is true UTC time before skew. Locator keys:
  `<source_id>:<file name>:line:<n>` (1-based within that file),
  `src_wms_db:purchase_orders:row:<id>`, `src_ship_sftp:file:<file name>`. `txn_id` is null for
  noise, file arrivals and export-log lines; `batch_id` is set on records that carry the batch key
  (file arrivals, `export_finished`/`upload_complete`, shipment and pickup lines). Every non-null
  `batch_id` has a `BatchTruth`; day 9's failed export lines carry no batch because that file never
  lands. `actor_kind` is `human` for clerk rows, `service` for automated lifecycle records, null
  for noise and file arrivals. `is_error` is true only for `payments:error_503`,
  `orders:payment_failed` and `warehouse:upload_failed`.
- Nodes: `webstore:cart_created`, `webstore:checkout_completed`, `orders:order_created`,
  `orders:payment_requested`, `orders:payment_failed`, `orders:order_released`,
  `payments:authorized`, `payments:error_503`, `warehouse:po_created`,
  `warehouse:export_finished`, `warehouse:upload_complete`, `warehouse:upload_failed`,
  `shipping:file_arrived`, `shipping:shipment_created`, `shipping:carrier_pickup`, and
  `<system>:noise` for webstore, orders, payments, warehouse (export log) and shipping.
- `links.json`: L01 exact webstore `cart_id` ~ orders `cart_id`; L02 bridge `cart_id` ~
  `order_id`; L03 bridge `order_id` ~ `merchant_ref`; L04 exact `merchant_ref` ~ payments
  `merchantRef`; L05 exact `order_id` raw ~ warehouse `order_ref` `digits.0`; L06 composite
  (manual) payments `amount` ~ warehouse `order_total` with components amount, `timestamp` date ~
  `order_date`, `cardholderName` `phonetic.0` ~ `customer_name` `phonetic.0`, each with its
  measured agreement rate over clerk rows; L07 exact warehouse `po_num` ~ shipping `po_num`;
  L07b the same for `po_number` (only when the rename is active); L08 bridge `shipment_no` ~
  `po_num`; L09 batch `file_name` ~ shipping `file` (entity `ent_shipping_file`); L10 bridge
  `shipment_no` ~ `manifest_id` (role batch, entity `ent_manifest`).
- `entities.json`: `ent_order` (every identifier field with its form, `po_number` only when the
  rename is active), `ent_shipping_file`, `ent_manifest`.
- `batches.json`: `batch_SHIP_YYYYMMDD_HHMM` keyed by `src_ship_sftp/file_name` and
  `batch_MAN-YYYYMMDD-01` keyed by `src_ship_log/manifest_id`, each with sorted `txn_ids`.
- `faults.json`: the table above (`start`/`end` in UTC).
- `manual_hops.json`: `hop_payments_to_warehouse` (manual, share 0.3, the 8 clerks, typo rate
  0.02) and the automated hops `hop_webstore_cart_to_checkout`, `hop_webstore_to_orders`,
  `hop_orders_created_to_payment_requested`, `hop_orders_to_payments`, `hop_payments_to_orders`,
  `hop_warehouse_to_shipping`, `hop_shipping_file_to_shipment`, `hop_shipping_shipment_to_pickup`.
- `markers.json`: `marker_tokens` (every `mk` + 8 lowercase hex token embedded in an emitted email
  or address, embedded unchanged so `token in value` holds for each one),
  `pii_values` (every emitted `customer_name`, `customer_email`, `cardholderName` and
  `ship_to_address`), `identifier_values` keyed by `str(FieldRef)` such as
  `sys_orders/src_orders_log/order_id` (only fields that appear in the data have a key).
- `manifest.json`: counts per source plus `transactions`, `events` (records with a transaction),
  `noise`, `files`; sha256 of every native file by relative path; the request parameters;
  `generator_version` from `carto_simulator.api.GENERATOR_VERSION`.

`ground_truth/` sits inside the output directory next to the native data. Point connectors and
offline bundles only at the per-system subdirectories (`webstore/`, `orders/`, `payments/`,
`warehouse/`, `shipping/`), never at the whole tree: `markers.json` holds the synthetic PII in
clear and `event_txn.ndjson` the transaction mapping the eval must not see through the engine.

## Determinism

Every random draw comes from one `random.Random(seed)` consumed in a fixed order: cart times per
day, then each transaction's lifecycle in cart order, then per-day export parameters, then the
nightly exports day by day (per-PO carrier, service, weight and shipment delay), then noise source
by source. Nothing reads the wall clock, uses `uuid`, `os.urandom` or iterates a set. The same
request produces byte-identical native files and ground truth on every platform; the generated
`README.md` quotes the seed and the sha256 of `event_txn.ndjson`, and `manifest.json` carries
the digest of every native file. The generator opens no network connection and, apart from the
tz database, reads nothing outside its own output directory.

Local time comes from `zoneinfo`. The IANA database is the system's where one exists and the
`tzdata` package otherwise (a declared dependency of this package, ADR 0003), so Windows and
minimal containers compute the same instants as Linux. `carto_simulator.clock` raises a clear
error naming `tzdata` if the zone cannot be loaded.

## Choices where the spec is silent

- Daily volume is the expectation exactly; weekends use `ceil(daily_volume / 2)`.
- Addresses embed the marker token as drawn (`282 mk5349da48 Street`), not capitalised, so every
  recorded token is an exact substring of the value it marks (the `MarkerSet` contract).
- `order_date` is the local date of `orders:order_created`.
- Picking outside warehouse hours resumes at the next 06:00 opening (the same day when the pick
  would fall before opening), plus U(0, 60) min.
- SHIP file names use the warehouse's own clock (true time + 90 s); the arrival mtime is true time.
- The rename split (F4) is by true `created_at`; rendered `created_at` therefore reads from
  00:01:30 on the rename day for the first renamed rows.
- F2's twelve error lines are evenly spaced between max(export + 30 s, 21:13:00) and 21:19:00 true
  time, so they always follow the export line; rendered they read 90 s later.
- Payment failure lines are logged 50 to 500 ms after the 503 response; retries wait U(5, 10) min
  after that line.
- Noise counts per hour are Poisson draws around the expectation.
- Payment methods: card 80%, paypal 12%, apple_pay 8%; shipment weights U(0.2, 30) kg.

## Tests

`uv run pytest simulator/tests` (file prefix `test_sim_`). The suite checks formats with
independent parsers (json, a logfmt regex, `xml.etree`, sqlite3), the faults day by day, that every
locator key resolves, the leak-test markers, determinism, the CLI and the request contract. The
statistical test (14 days by 200 orders, clerk share 0.30 +/- 0.08, typo share 0.02 +/- 0.02) is
marked `slow`.
