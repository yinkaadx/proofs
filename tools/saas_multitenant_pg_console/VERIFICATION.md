# Verification transcript

Every behaviour reproduced by `core.py` was run against a live server before
the engine was written. Server: PostgreSQL 16.13 (Ubuntu 16.13-0ubuntu0.24.04.1),
temporary cluster, trust auth, unix socket.

## Row Level Security semantics

| Probe | Observed |
|---|---|
| Owner, `ENABLE ROW LEVEL SECURITY` only, context set to a tenant owning 2 of 3 rows | **3 of 3 rows** |
| Non owner, same policy, same context | 2 of 3 rows |
| Owner, after `FORCE ROW LEVEL SECURITY` | 2 of 3 rows |
| Second policy added, `FOR SELECT USING (true)`, permissive by default | **3 of 3 rows**, up from 2 |
| Same policy declared `AS RESTRICTIVE` | 2 of 3 rows, unchanged |
| Only restrictive policies on the table | **0 of the tenant's own 2 rows** |
| `BEGIN; SET LOCAL app.tenant_id='acme'; COMMIT;` then read it | empty |
| `BEGIN; SET app.tenant_id='globex'; COMMIT;` then read it | `globex`, still set |
| `current_setting('app.never_set')` | `ERROR: 42704 unrecognized configuration parameter` |
| `current_setting('app.never_set', true)` | NULL |
| `SET app.x='v'; RESET app.x; current_setting('app.x')` | the empty string, no error |
| Insert of another tenant's row under `FOR ALL` with only `USING` | `ERROR: new row violates row-level security policy` |

## The shipped DDL, executed

`get_sample_rls_ddl()` was written to a file and run with
`psql -v ON_ERROR_STOP=1`. It completed with no errors. Then, connected as
`app_user`:

| Statement | Result |
|---|---|
| `SET LOCAL app.tenant_id='acme'; SELECT count(*)` | `acme_sees=2` |
| `SET LOCAL app.tenant_id='globex'; SELECT count(*)` | `globex_sees=1` |
| As acme, `INSERT ... VALUES ('globex','stolen')` | `ERROR: new row violates row-level security policy` |
| No context set, `SELECT count(*)` | `no_context_sees=0` |
| `SET LOCAL app.tenant_id=''; SELECT count(*)` | `empty_context_sees=0` |

## A bug this caught

The first draft of the DDL declared both policies `AS RESTRICTIVE`. The tenant
then saw zero of its own rows, because restrictive policies only narrow what a
permissive policy already granted and there was nothing to narrow. The shipped
script makes the isolation policy permissive and the context guard restrictive.
That correction exists because the script was run rather than reasoned about.
