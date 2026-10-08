# Open the owner portal to your class

As the operator, use this guide to open the owner portal at `https://<domain>`, create the first portal admin, and check student sign-in. Complete [Deploy the platform](deploy-the-platform.md) first.

## How the portal works

The portal runs on the admin host as three services:

- `management-web` serves the website on port 8080, reached through ingress. It keeps no state.
- `management-broker` stores accounts, sessions, app ownership, limits, and audit records in SQLite on the admin state volume. It has no network access and talks to the controller through `/run/<namespace>-controller/project.sock`.
- `management-identity` exchanges sign-in codes with Commons. It's the only portal service with outbound network access.

You install a matched pair: the broker release (including identity) and the web release. A root activation unit switches releases only when both halves match.

### How people sign in

[Commons](../README.md#glossary) is a separate class website. **Sign in with your class account** sends the browser there for approval. Commons returns a single-use code, which the identity service exchanges for the person's identity. The portal never sees Commons passwords. First sign-in creates an owner account with default limits.

Local accounts live only in the portal. A portal admin sends a setup link, and the person chooses a password. The broker stores a salted scrypt password hash. Owners and staff can add an authenticator app (TOTP).

Portal admins must be local accounts with an authenticator app. Commons accounts can be owners or staff. The operator prints the first admin's setup link on the admin host, so no initial password sits in configuration.

Sessions are stored on the server:

| Role | Session ends after | Or after being idle for |
| --- | --- | --- |
| Owner | 8 hours | 30 minutes |
| Staff | 1 hour | 10 minutes |
| Portal admin | 1 hour | 15 minutes |

Owner times default to `absoluteSeconds` and `idleSeconds` in the inventory (step 2). Shorter owner times also shorten staff and admin sessions. Sessions keep the role assigned at sign-in; role changes, disabling an account, and credential resets end its sessions.

Commons password changes, account archival, and revoked approval don't end existing portal sessions. To cut off access, use **Disable account**. **Sign out everywhere** ends sessions but allows the person to sign in again. See [Manage accounts](manage-apps-and-people.md#help-a-person).

## Before you start

- The platform is healthy and your shell has the operator variables from [Set up your shell](run-the-platform.md#set-up-your-shell). The commands below use `$PLATFORM_CONFIG`, `$SSH_CONFIG`, `$PLATFORM_NAMESPACE`, `$PLATFORM_ADMIN_STATE`, `$PLATFORM_DOMAIN`, and the `recovery` function defined there.
- You have a checkout of this repository on the operator host, with `uv`, at the commit you're releasing.
- You have a signed portal release for that commit: the broker and web archives plus their release evidence. [Build and sign the portal release](releases-and-upgrades.md#build-and-sign-the-portal-release) explains how to build and verify one. Build it from the same commit as your admin image and helper, so their compatibility matches.
- You've decided how students sign in (see [Sign-in for the owner portal](plan-a-deployment.md#sign-in-for-the-owner-portal)). For Commons, you have access to its settings.
- You have an authenticator app for the first portal admin.

## 1. Prepare Commons

Commons sends sign-in codes only to app origins it trusts. On your Commons deployment, set:

```text
CONNECT_APP_DOMAIN=<domain>
```

Use the platform domain without a scheme or path. Commons accepts `https://<domain>` and apps one label below it, such as `https://<name>.<domain>`, but excludes its own origin. Commons can itself run as an app on this platform. See its [origin check](https://github.com/mit-sdg/commons/blob/main/src/computations/connect.ts) and [deployment instructions](https://github.com/mit-sdg/commons).

Write down two things for step 2:

- The Commons origin, such as `https://class.example.edu`: a public HTTPS origin with no path. If Commons runs on this platform, use its app URL.
- Commons' address ranges for `identityEgressCidrs`. An empty list allows Cloudflare's published ranges, suitable for Commons served through Cloudflare. Otherwise, list the ranges the identity service needs to reach Commons.

> [!NOTE]
> **Local accounts only.** The inventory still needs a `commonsOrigin` once the portal is enabled, and the sign-in page always shows the Commons button. Without a working Commons, that button fails, and everyone uses **Use a local account** with accounts a portal admin creates.

## 2. Turn on the portal in the inventory

Setup creates the inventory with the portal turned off. Add an `ownerPortal` section to your deployment inventory (see [Generated inventory](../reference/configuration.md#generated-inventory) for where its copies live):

```json
"ownerPortal": {
  "enabled": true,
  "commonsOrigin": "https://class.example.edu",
  "identityEgressCidrs": [],
  "classLabel": "class account",
  "portalName": "Example Course Apps",
  "appLimit": 2,
  "concurrencyLimit": 1,
  "absoluteSeconds": 28800,
  "idleSeconds": 1800
}
```

| Field | What to set |
| --- | --- |
| `enabled` | `true` to turn the portal on. |
| `commonsOrigin` | Your Commons origin from step 1. Required once the portal is enabled. The portal also uses it to recognize the Commons app. |
| `identityEgressCidrs` | Leave `[]` to allow Cloudflare's ranges, or list Commons' address ranges (up to 64, no `/0`). |
| `classLabel` | Optional. The sign-in button reads **Sign in with your** followed by this label (default `class account`). |
| `portalName` | Optional. The name in the portal's header (default `<displayName> Apps`). |
| `appLimit`, `concurrencyLimit` | Optional. Each owner's default number of apps (default 2) and changes in progress at once (default 1). |
| `absoluteSeconds`, `idleSeconds` | Optional. Owner session lengths (defaults 8 hours and 30 minutes). |
| `anonymousStartsPerMinute` | Optional. Commons sign-in attempts allowed per client address per minute (default 400). Raise it if a whole class signs in from one network address. |

[The owner portal section](../reference/configuration.md#the-owner-portal-section) of the configuration reference lists every field with its allowed range.

The admin image carries `/etc/<namespace>/platform.json` and the identity network allowlist. Build images from the updated inventory, accept the admin image, and [replace the admin host](hosts-and-images.md#replace-the-admin-host). Install operator and helper releases from the same commit ([Releases and upgrades](releases-and-upgrades.md)).

Check the new admin host's configuration. This validates `ownerPortal` and writes files for inspection without changing running releases:

```bash
ssh -F "$SSH_CONFIG" platform-admin -- openstack-platform-management-config \
  --platform-config "/etc/$PLATFORM_NAMESPACE/platform.json"
```

You should see `management-config=rendered development=false`. If it says `ownerPortal is disabled`, the admin host is still running an image built from the old inventory.

## 3. Install the portal release

This is the first installation. For later upgrades, follow [Upgrade the owner portal](releases-and-upgrades.md#upgrade-the-owner-portal), which adds pausing changes and taking backups first.

Install the broker, then the web component. The second install requests activation. The root unit `<namespace>-management-activate.service` checks the pair, selects it, and restarts all three services. If installation fails before activation, the previously selected pair stays in place.

1. In your repository checkout on the operator host, set the release you're installing. `EVIDENCE` is the directory the release build wrote, containing `broker-<commit>.tar`, `web-<commit>.tar`, `release-manifest.json`, `management-artifacts.json`, their `.sig` files, and the SBOM and provenance files.

   ```bash
   COMMIT=<FULL_COMMIT>
   EVIDENCE=<PATH_TO_RELEASE_DIRECTORY>
   TRUST_ROOT=<PATH_TO_RELEASE_TRUST_ROOT_PEM>
   IDENTITY_SHA256="$(uv run python -c 'import sys; from openstack_platform.config import load_platform, platform_config_identity; print(platform_config_identity(load_platform(sys.argv[1])))' "$PLATFORM_CONFIG")"
   ```

   `IDENTITY_SHA256` is the deployment identity: a hash of the inventory's stable fields (project, names, addresses, paths). The installer refuses a release meant for another deployment.

2. Copy the release and the trust root to the operator account on the admin host. If an older `portal-release` directory is there, remove it first, or `scp` copies into it.

   ```bash
   scp -F "$SSH_CONFIG" -r "$EVIDENCE" platform-admin:portal-release
   scp -F "$SSH_CONFIG" "$TRUST_ROOT" platform-admin:release-trust-root.pem
   ```

3. Install both halves. The archive hashes come from the verified `management-artifacts.json`:

   ```bash
   install_portal() {
     sha256="$(uv run python -c 'import json, sys; print(json.load(open(sys.argv[1]))["archives"][sys.argv[2]]["sha256"])' \
       "$EVIDENCE/management-artifacts.json" "$1")"
     ssh -F "$SSH_CONFIG" platform-admin -- openstack-platform-install-release \
       --mode "$1" \
       --archive "portal-release/$1-$COMMIT.tar" --archive-sha256 "$sha256" \
       --commit "$COMMIT" \
       --platform-config "/etc/$PLATFORM_NAMESPACE/platform.json" \
       --expected-platform-namespace "$PLATFORM_NAMESPACE" \
       --expected-platform-identity-sha256 "$IDENTITY_SHA256" \
       --release-manifest portal-release/release-manifest.json \
       --release-signature portal-release/release-manifest.sig \
       --release-trust-root release-trust-root.pem \
       --management-manifest portal-release/management-artifacts.json \
       --management-signature portal-release/management-artifacts.sig
   }
   install_portal broker
   install_portal web
   ```

   Each install authenticates the evidence and the archive, unpacks the release into its own directory, starts the candidate once in a smoke-test mode (without the live database or network), and only then selects it.

4. Check that the pair is active. Give the activation unit a few seconds first.

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin -- readlink -f \
     "$PLATFORM_ADMIN_STATE/management-active/current/broker" \
     "$PLATFORM_ADMIN_STATE/management-active/current/web"
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl show -p Result \
     "$PLATFORM_NAMESPACE-management-activate.service"
   ssh -F "$SSH_CONFIG" platform-admin -- systemctl is-active \
     "$PLATFORM_NAMESPACE-management-identity.service" \
     "$PLATFORM_NAMESPACE-management-broker.service" \
     "$PLATFORM_NAMESPACE-management-web.service"
   curl --fail --show-error --silent "https://$PLATFORM_DOMAIN/auth/options"
   ```

   You should see two paths under `management-broker-releases/releases/` and `management-web-releases/releases/` whose names start with your commit, `Result=success`, three lines of `active`, and a JSON response that includes `"providerLabel"`.

If the active paths didn't change, read the activation unit's journal from the recovery console (`recovery sudo journalctl -u <namespace>-management-activate.service -n 100 --no-pager`). Fix the cause it names (a mismatched pair, missing evidence, wrong ownership or mode, or an incompatible schema) and install again. The previous pair keeps serving in the meantime.

## 4. Turn on portal backups

The broker database holds accounts, roles, ownership, and audit records. `<namespace>-management-broker-backup.timer` encrypts it daily around 02:30 UTC to your backup age recipient. Without the recipient file on the admin host, backups are skipped even though systemd reports success.

1. Run this on the operator host. It reads `backupAgeRecipient` from the installed operator policy and writes the recipient file on the admin host over SSH. Both `/srv/openstack-platform` paths before the pipe are on the operator host:

   ```bash
   /srv/openstack-platform/runtime/python3.14 -c \
     'import json; print(json.load(open("/srv/openstack-platform/state/policy.json"))["backupAgeRecipient"])' |
     ssh -F "$SSH_CONFIG" platform-admin -- tee \
     "$PLATFORM_ADMIN_STATE/management-broker-releases/config/backup-recipient.txt"
   ```

   It prints one line starting with `age1`.

2. Take the first backup now instead of waiting for the timer (root):

   ```bash
   recovery sudo systemctl start "$PLATFORM_NAMESPACE-management-broker-backup.service"
   recovery sudo journalctl -u "$PLATFORM_NAMESPACE-management-broker-backup.service" -n 5 --no-pager
   ```

   The journal shows `management-broker-backup=management-broker-<timestamp>.sqlite3.age sha256=<checksum>`.

[Verify backups and schedules](backups-and-recovery.md#verify-backups-and-schedules) includes this check in the routine backup review, and [Restore the owner portal database](backups-and-recovery.md#restore-the-owner-portal-database) covers restores.

## 5. Create the first portal admin

Print a single-use setup link on the admin host. It expires after 24 hours; a new link cancels earlier unused links. Send it privately to the designated portal admin and keep it out of tickets and chat.

1. Open a shell on the admin host as the operator account (the command refuses to run as root):

   ```bash
   ssh -F "$SSH_CONFIG" platform-admin
   ```

2. On the admin host, set `STATE` to the inventory's `paths.adminState` (the value of `$PLATFORM_ADMIN_STATE` on the operator host), then run the setup command from the active broker release, so it matches the running broker:

   ```bash
   STATE=<ADMIN_STATE_PATH>
   BROKER_RELEASE=$(readlink -f "$STATE/management-active/current/broker")
   /run/current-system/sw/bin/management-python3.14 -I -B \
     "$BROKER_RELEASE/runtime/openstack_platform/management/entry.py" bootstrap \
     --config "$BROKER_RELEASE/config/management.json" \
     --requirements "$BROKER_RELEASE/requirements.json"
   ```

   It prints a link like `https://<domain>/setup#<id>.<token>`. The admin image also has a shorter form, `openstack-platform-management-bootstrap --config "$BROKER_RELEASE/config/management.json"`, which uses the image's copy of the code.

3. Open the complete link in a browser. The page, **Set up your admin account**, removes the secret part from the address bar straight away.
4. Choose a **Username** (3 to 32 characters: lowercase letters, digits, `.`, `_`, or `-`, starting with a letter), an optional **Display name**, and a **New password** (at least 12 characters, at most 1024 bytes, not containing the username). Select **Continue**.
5. On **Add your authenticator**, scan the QR code or enter the key in your authenticator app. The key is shown only this once. Enter the 6-digit **Authentication code** and select **Finish setup**. You have 10 minutes and five tries.

You're now signed in as a portal admin, on **People**.

If setup is abandoned or uses all five tries, print a new link and choose another username. The unfinished account keeps its username reserved; you can disable it later.

## 6. Add staff and set limits

As the portal admin, open **People**:

- **Staff with local accounts.** Select **Create account**, set **Role** to **Staff**, and send the **Setup link** privately. It works once and expires in 72 hours. The portal sends no email.
- **Staff who use Commons.** Ask them to sign in once, so their account exists. Then open their account, set **Role** to **Staff**, and select **Change role**.
- **Limits.** Owners get `appLimit` and `concurrencyLimit` from the inventory. To give one owner more or less, open their account and change **My apps** or **Changes at a time** under **Limits**.

Creating accounts and changing roles ask for your password and a fresh authenticator code, unless you confirmed them in the last five minutes. [Manage apps and people](manage-apps-and-people.md) covers roles, limits, and the rest of the account tools.

## 7. Check that students can sign in

1. Open `https://<domain>` in a private browser window.
2. Select **Sign in with your class account** (with your `classLabel`). Commons asks you to approve the portal the first time.
3. You should land on **My apps**. As the portal admin, you'll now see that account in **People** with the role **Owner**.

If sign-in doesn't finish, the portal returns to the sign-in page with a message:

| What you see | What it means | What to do |
| --- | --- | --- |
| Commons shows an error and doesn't ask for approval | Commons doesn't accept the portal's origin. | Set `CONNECT_APP_DOMAIN` on Commons to your platform domain (step 1). |
| Sign-in was cancelled. | The person chose Cancel on Commons. | Try again. |
| That sign-in expired. Try again. | Commons refused the code (already used, expired, approval withdrawn, or the person was archived), or the browser took more than 10 minutes to come back. | Try again. If it keeps happening for everyone, check that the inventory's `commonsOrigin` is the Commons that people approve on. |
| Signing in with your class account is unavailable right now. Try again in a minute. | `management-identity` couldn't reach Commons or got an unexpected answer. | Read its journal (`recovery sudo journalctl -u <namespace>-management-identity.service -n 100 --no-pager`). Lines read `identity-check=unavailable reason=<reason>`. A `connect:` reason points at the network: check `identityEgressCidrs` and that Commons answers over HTTPS. |
| This account is disabled or archived. Contact staff if you think this is a mistake. | The portal account is disabled. | A portal admin enables it in **People**. |
| Too many attempts. Wait a minute, then try again. | The address made too many sign-in attempts in a minute. | Wait a minute. For a class behind one shared address, raise `anonymousStartsPerMinute`. |

## Portal startup and controller restarts

You don't need to start or restart the portal by hand:

- At boot, the portal services start as soon as an active pair exists.
- The broker starts only after `<namespace>-controller-readiness.service` confirms the controller answers. Restarting the controller restarts the broker and then the web service after it.
- If the broker or web process exits, systemd restarts it after two seconds.
- The broker wants the identity service but doesn't depend on it. If `management-identity` fails, Commons sign-in stops working, while existing sessions and local sign-in keep working.

## Recover a lost portal admin

If no one can sign in as a portal admin, create a new one with the same command as step 5. Choose a new username. The new admin can then open the old account in **People** and either disable it or send it a reset link.

To replace both the password and the authenticator of an existing account, send **Reset authenticator** first and let the person finish it, then send **Reset password**. Sending a second reset cancels any earlier reset link for that account.

Keep a local portal admin available for recovery. Commons accounts can't be portal admins, and staff can't start new Commons sessions while Commons is down. Existing portal sessions keep working.

## Change portal settings later

Each installed release keeps its own snapshot of the inventory, and the release directory's name includes a hash of that snapshot. To change any `ownerPortal` setting:

1. Update the inventory and roll out a new admin image, as in step 2.
2. Install the broker and the web component again, as in step 3. You can reuse the same archives; the new inventory gives them new release directories.

The new settings take effect when the new pair activates.

## Roll back to a retained portal pair

Retained releases let you return to an earlier pair without reinstalling. The command verifies signatures, files, ownership, and inventory snapshots, smoke-tests both releases, and requests activation. The pair returns with its own portal settings.

Before you start, pause changes and take fresh broker and controller backups ([Backups and recovery](backups-and-recovery.md#verify-backups-and-schedules)). Review the pair's sign-in settings and database migration evidence. The command requires compatible retained and active release descriptors (currently schema 3, protocol 3, controller API 1); it can't inspect the private broker database as the operator. An incompatible database needs a reviewed forward repair or verified offline restore. A restore ends sessions and may require reconciling controller and ownership records from different backup times.

1. On the admin host (`ssh -F "$SSH_CONFIG" platform-admin`), list the retained releases. Each name is `<commit>-<pair>-<inventory>`.

   ```bash
   STATE=<ADMIN_STATE_PATH>
   NS=<NAMESPACE>
   ls "$STATE/management-broker-releases/releases" "$STATE/management-web-releases/releases"
   ```

2. Request the pair you want, using the full directory names:

   ```bash
   BROKER_RELEASE=<BROKER_RELEASE_DIRECTORY_NAME>
   WEB_RELEASE=<WEB_RELEASE_DIRECTORY_NAME>
   openstack-platform-management-reactivate \
     --platform-config "/etc/$NS/platform.json" \
     --broker-release "$BROKER_RELEASE" --web-release "$WEB_RELEASE"
   ```

   It prints `management-reactivation=requested broker=<name> web=<name>`. That means the request was written, not that activation succeeded. If validation fails, nothing changes.

3. Check the result:

   ```bash
   test "$(readlink -f "$STATE/management-active/current/broker")" = \
     "$STATE/management-broker-releases/releases/$BROKER_RELEASE" && echo broker-ok
   test "$(readlink -f "$STATE/management-active/current/web")" = \
     "$STATE/management-web-releases/releases/$WEB_RELEASE" && echo web-ok
   systemctl show "$NS-management-activate.service" -p Result
   systemctl is-active "$NS-management-broker.service" "$NS-management-web.service"
   ```

   You should see `broker-ok`, `web-ok`, `Result=success`, and `active` twice. Take a new broker backup once the portal works.

## Related

- [Manage apps and people](manage-apps-and-people.md): roles, accounts, limits, and managing any app
- [Deploy an app](for-app-owners.md): the guide to share with students
- [Releases and upgrades](releases-and-upgrades.md): building and signing portal releases
- [Hosts and images](hosts-and-images.md): rolling out an admin image with a changed inventory
- [Backups and recovery](backups-and-recovery.md#restore-the-owner-portal-database): restoring the portal's database
- [Security](../security.md#the-owner-portal): what the portal's boundaries protect
