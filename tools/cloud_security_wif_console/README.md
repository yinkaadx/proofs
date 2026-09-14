# EHR Cloud Security & WIF Architecture Console

A console for the Azure plus GCP side of an electronic health record platform.
It walks an Azure managed identity all the way to a short lived GCS token with
no key anywhere on disk, holds a downscoped token inside one tenant's prefix and
shows the cross tenant attempt being refused, decides where a vault is genuinely
required and where a stored secret should not exist at all, and takes a static
service account key apart vector by vector.

Live at `/cloud-security-wif-console` on the hub.

## The four questions it answers

**How does Azure compute get a GCP token without a key?** The simulator runs the
real sequence: `ManagedIdentityCredential` acquires an Entra ID token, that
assertion is exchanged at the GCP STS token endpoint for a federated token, and
that token impersonates the target service account through IAM Credentials to
produce the GCS access token. Every step can fail for a named reason, and the
failures are the point: a wrong audience, a wrong issuer, an expired assertion,
a subject that does not match the pool's attribute mapping, an attribute
condition that rejects the principal, or a missing `roles/iam.workloadIdentityUser`
binding on the target service account. A failing step stops the flow and no
token is issued, which is exactly what should happen and exactly what a diagram
cannot demonstrate.

**How is one tenant kept out of another tenant's records?** A Credential Access
Boundary downscopes the token to a single prefix, expressed the way GCS actually
expresses it: `availableResource`, `availablePermissions` as roles, and an
`availabilityCondition` whose CEL calls `resource.name.startsWith(...)`.

The evaluator exists because of one specific trap. CEL's `startsWith` is a byte
prefix test with no path awareness, so a boundary written on `tenants/tenant-a`
also authorizes `tenants/tenant-a-archive` and `tenants/tenant-abc`. That is a
cross tenant breach of patient records produced by a missing trailing slash. The
engine therefore carries both matchers and reports both verdicts per path, so
the console can show the same request returning 403 under the correct boundary
and 200 under the naive one. Every denial names its reason rather than returning
a bare false.

**Where does Key Vault belong, and where should a secret not exist?** The matrix
separates the two honestly. A vault is mandatory for anything the platform
cannot mint, a third party API key or a client certificate. For Azure SQL,
Storage, Event Grid and GCS the answer is not a better vault but no stored
secret at all, and each row names the mechanism that replaces it.

**Why is the static JSON key on IIS the real problem?** The inspector breaks it
into the vectors that actually get exploited: exposure through the file system
and through backups, a lifetime that never ends because nothing rotates it, the
loss of identity non repudiation once every action in the audit log is attributed
to the service account rather than to a person, and extraction from process
memory or a crash dump. Each vector carries its severity, how it is exploited,
and what federation replaces it with.

## Layout

| File | Purpose |
| --- | --- |
| `core.py` | The engine: the federation flow, the boundary evaluator, the policy matrix and the failure mode catalogue. No Streamlit import |
| `page.py` | The Streamlit page, exposing `render()` |

## Safety

- Every STS and IAM Credentials request is built in full and displayed rather
  than sent, so the console cannot touch a live tenant or project.
- Tokens and assertions are redacted in the generator, not at the point of
  display, so a caller that forgets cannot leak one.
- Anything typed into the page is HTML escaped before it reaches an
  `unsafe_allow_html` block.

## Tests

```bash
python3 -m pytest tests/test_cloud_security_wif_console.py \
                 tests/test_cloud_security_wif_console_page.py -q
```

Both suites also run standalone with `python3`, printing
`WIF RESULT: PASS <n>/<n>` and `WIF UI RESULT: PASS <n>/<n>`.
