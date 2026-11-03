# Separate database write privileges for authorization maintenance

Status: accepted design direction; the overall technical design remains in draft.

The daily business service reads current account status, roles, and resource grants but cannot modify those authorization records through its database connection. Explicit initialization and the controlled local maintenance command use a separate connection limited to the authorization maintenance objects. The local operator identity and a change identifier are auditable; maintenance credentials stay in local secret configuration. This choice adds a credential and migration boundary but avoids giving the daily service connection the capability to change its own users' grants. It does not add a business administrator role or require row-level security.

The exact database privileges, host operator identity mapping, and audit write rights remain design work; this ADR does not claim that the roles have been configured or tested.
