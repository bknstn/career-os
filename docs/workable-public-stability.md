# Workable public endpoint stability

Career OS uses only the credential-free alternate public endpoint:

`GET https://www.workable.com/api/accounts/{account_subdomain}?details=true`

It does not call Workable's authenticated, versioned SPI and sends no
`Authorization` header. This distinction is intentional: credentials and SPI
contracts are out of scope for this adapter. Workable presents this as an
alternate public endpoint in its [careers-page API reference](https://help.workable.com/hc/en-us/articles/115012771647-Using-the-Workable-API-to-create-a-careers-page).

The adapter is default-off. Enable it deliberately with
`CAREER_OS_ENABLE_WORKABLE_PUBLIC=true`, then validate a configuration that
contains `kind: workable_public`. The public response uses counterintuitive URL
names: `application_url` is retained as the job-page `source_url`, `shortlink`
is only its fallback, and `url` is retained as the application-form `apply_url`.

This endpoint is unversioned and supplies no total, pagination, or explicit
completeness marker. A successful object with a structurally safe `jobs` list is
therefore the current, weaker completion assumption. Schema errors, malformed
jobs, and duplicate shortcodes fail closed and yield a partial scan. A
structurally valid empty list is still the adapter's weaker completeness claim,
but the operational safeguard downgrades it to a partial run when the latest
successful scan was non-empty. Large count drops are guarded the same way, so
neither case can inactivate unseen jobs without operator review.
