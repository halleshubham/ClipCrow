# Superadmin and operator management: audit and checklist

Last reviewed 2026-11-22. Roles: **superadmin** = an address in `CLIPCROW_ADMINS` (server environment, cannot be changed from the UI). **Operator** = added on the settings page (by a superadmin). **Person** = anyone else.

## What each role can do
| | Person | Operator | Superadmin |
|---|---|---|---|
| Operator page, people list, storage numbers, ledger of one account | no (404) | read | read |
| Disable / enable an account | no | yes (audited) | yes |
| Add or remove minutes, change a plan | no | no | yes (reason required, max 10,000 minutes at a time) |
| Change any setting (prices, limits, retention, operator list, free minutes) | no | no (read only) | yes |
| View an account as its owner (read-only, silent, 1 hour) | no | no | yes |
| Remove local clip files, upload missing copies, give old jobs to an account | no | no | yes |
| Read the audit log | no | no | yes |

## Done (best practice -> how it is met)
- **Least privilege:** money and configuration are superadmin-only; operators are support staff who can look and disable. Superadmins are defined outside the app (environment), so no UI path can create one.
- **One append-only audit trail:** every privileged action (credits, plans, disable/enable, settings, storage actions, giving jobs, view-as start/stop) is written to `admin_audit` with who, what, which account and the source address, BEFORE the action runs (if it cannot be written, the action does not happen). There is no route to edit or delete it.
- **A reason for every money change**, kept in the account's own ledger and in the audit log; a cap per change.
- **Alerts:** mail to `CLIPCROW_ALERT_EMAIL` when the operator list changes, when 1,000+ minutes move at once, and when an account is viewed.
- **Short privileged sessions:** a superadmin session lasts 7 days (everyone else 30).
- **Cannot lock yourself or the owners out:** you cannot disable yourself or a superadmin; view-as cannot target a superadmin.
- **Impersonation is contained:** read-only, no social posting or connections, one hour, ends at sign-out, disclosed on the privacy page.
- **Hidden surface:** operator routes answer 404 to everyone else.

## Not done yet (recommended order)
1. **A second factor for superadmins** (a passkey or an authenticator code). Today the emailed code is the only factor, so whoever controls a superadmin's mailbox controls the service. Highest value.
2. **Step-up for dangerous actions** (ask for a fresh emailed code before credits, settings, view-as, clean-ups).
3. **At least two superadmins** in `CLIPCROW_ADMINS` (a break-glass account, kept offline) so one lost mailbox does not lock you out or leave a single point of failure.
4. **Dual control** for very large changes (a second superadmin approves) once there is a second superadmin.
5. **Idle timeout** for superadmin sessions (today a fixed 7 days) and "sign out everywhere" in the UI.
6. **Export of the audit log** to somewhere outside the database (a daily copy to R2) so it survives a database restore to an older state.
7. **Review cadence:** read the audit log weekly; remove operators who left.
