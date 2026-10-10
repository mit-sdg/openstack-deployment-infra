# Manage apps and people

Use the portal to manage apps across the class and help people finish blocked changes. This guide is for staff and portal admins. Staff share the app controls; whole-app deletion, storage-limit changes, account controls, platform settings, and the audit log require an admin.

## Choose a destination

| Destination | URL | Who sees it | What it shows |
| --- | --- | --- | --- |
| **My apps** | `/apps` | Everyone | Apps you own or work on as a teammate |
| **All apps** | `/all-apps` | Staff and portal admins | Every managed app, with search by app or owner and a status filter |
| **People** | `/people` | Staff and portal admins | Everyone, including owners, staff, admins and accounts awaiting setup; each person’s apps and activity |
| **Activity** | `/activity` | Staff and portal admins | Class-wide changes, with blocked and unknown changes at the top |
| **Audit log** | `/audit` | Portal admins | Account changes and app-management actions |
| **Platform settings** | `/platform-settings` | Portal admins | The default build machine |

A portal admin lands on **People** after signing in. Other accounts land on **My apps**.

## Open and manage an app

1. Open **All apps**.
2. Search by app name, owner name, or username. Use **Status** to filter the full catalog, including **Needs attention**.
3. Select the app to open **Overview**.

An app appears after it is created in the portal or adopted. Its slug is the permanent app name in its URL. Lists contain 25 apps per page.

The **Status** column shows blocked or unknown changes below the app state. Hover over the app state to see when it was last checked. The list refreshes automatically; observations older than ten minutes show **Unknown** while a new check runs.

Staff and portal admins open the same app pages as owners without joining the team:

- **Overview** shows the current deployment, activity and start, stop and restart controls. Staff and admins also see **Machines**, with the worker and effective build machine.
- **Settings** holds repository and runtime settings, deploy keys, environment variables, databases and storage. Staff and admins additionally get **Danger zone**, with **Change owner** and storage deletion. Only admins see **Delete app**.
- **Deploy** lets you choose a commit, review it, and deploy. Staff and admins additionally get the deployment method, worker size, and a read-only build machine summary.
- **Deployments** lists attempts. Open one to see its details and build output.
- **Logs** shows runtime output and errors.
- **Team** lets owners, staff and admins add or remove teammates. Members can leave their own team.

The optional **Build script** has no default. Leave it empty when the app has no build step. Environment values stay write-only for everyone. See [Deploy an app](for-app-owners.md) for the shared controls.

Staff and admins have no app or concurrency limit. Creating an app for another person uses that owner's app limit. Each app permits only one foreground infrastructure change at a time: that change must settle before another starts. Accepted deployment cleanup can continue in the background while compatible changes proceed.

Only portal admins can edit storage limits. Staff and admin app changes appear under their usernames in **Audit log**; their reads go into a separate private read log.

## Resume a change that needs attention

**Needs attention** means a change is blocked; **Unknown** means its outcome has not been confirmed. Both offer **Resume** when the change can be replayed. Find the same status and action on **Overview**, deployment lists and details, **All apps**, **Activity**, or the person’s apps and activity under **People**.

Select **Resume** to continue the original request with its original commit, settings and request keys. Before acceptance, finish the blocking change before starting another infrastructure change. After acceptance, cleanup retries automatically and compatible changes can proceed.

Staff and admins can resume ordinary app changes without re-authentication. Owners and teammates can resume ordinary changes on their apps; changes admitted through staff controls require staff or an admin. Only admins can resume storage-limit and default-build-machine changes. Whole-app deletion also requires recent re-authentication. The audit log records staff and admin Resume actions.

An unfinished environment edit requires the person who started it to enter the same value again in **Settings** > **Environment variables**. It has no generic Resume action because values are not stored for replay.

## Create, adopt, and move apps

On **All apps**, select **Create app**, enter the **App name**, choose an active, enabled **Owner**, and select **Create app**. The app opens on the shared Overview page; open **Settings** to configure it.

### Adopt an app the operator deployed

Adopt an app the operator [deployed from the command line](deploy-apps-from-the-command-line.md) to give it a portal owner.

1. Get the app's ID (a UUID) from the operator.
2. On **All apps**, select **Adopt app**. Enter the **App ID**. Leave **Owner** empty to own it yourself, or choose an account.
3. Select **Adopt app**.

Adoption imports the app's current accepted repository, branch, commit, and settings, including its storage connections. It doesn't restart, resize, or redeploy the app. The owner keeps the app even if it puts them over their limit; only new apps are limited.

Adoption is refused when:

- the app has no complete accepted deployment yet (`SNAPSHOT_UNAVAILABLE`). Have the operator deploy it first.
- the app already has a portal owner, or its name is taken (`ALREADY_OWNED`).
- the app's active deployment changed during adoption (`SNAPSHOT_CHANGED`). Try again.

### Change an app's owner

On the app's **Settings** page, under **Danger zone**, select **Change owner**, choose the new owner, and select **Change owner**. The portal refuses while the app has a change in progress (`APP_BUSY`), or if someone else changed the owner since you loaded the page (`OWNER_CONFLICT`; reload). If the new owner was a teammate, they're removed from the team list; other teammates stay. The new owner keeps the app even if it puts them over their limit.

### Delete an app (portal admins only)

Deleting an app permanently removes the running app and worker, every database and its data, the S3 bucket and objects, environment variables, the deploy key, and all team members. Recovery is possible only from platform backups and requires the platform team; changes since the backup are lost. The portal cannot currently read the last successful platform backup time. Check it with the platform team before deleting. Deleting the class sign-in app interrupts class sign-in.

1. Open the app's **Settings**, then **Delete app** in **Danger zone**.
2. Read the removal notice and type the exact app slug. Select **Delete permanently**.
3. Confirm your password and a fresh authenticator code if you have not re-authenticated in the last five minutes.
4. Watch the deletion progress in the dialog. If it needs attention, an admin can select **Resume** after re-authentication; it keeps the original controller key and resumes cleanup. Refreshing or closing the page does not cancel an admitted deletion. Find pending deletion in class **Activity**.
5. When **The app and its data have been deleted.** appears, select **Done**. The app disappears from app lists. Its tombstone and operation/audit history remain.

Owners, teammates and staff cannot admit or resume whole-app deletion. Deletion blocks other infrastructure changes until it settles. You do not need to remove storage bindings before whole-app deletion.

The portal calls broker `POST /v1/apps/{app}/delete` with the exact slug as `confirmation` and an idempotency key. The broker forwards the request to controller `POST /v1/applications/{id}/delete` on the project socket. The controller also exposes that route on the privileged socket for operators.

### Delete a database or storage

> [!WARNING]
> Deleting storage destroys the database or bucket and all its data, even if the bucket isn't empty. Recovery requires the operator and a managed-data backup; changes since the backup are lost.

1. Check that a recent managed-data backup exists ([Backups and recovery](backups-and-recovery.md)). Plan an outage if the running app uses this resource.
2. For an app using the resource, select **Stop app** on **Overview** and wait for it to stop. Prepare the code to run without the resource.
3. Under **Settings** > **Databases and storage**, open **Edit variables** for the resource. Remove every variable and select **Save variables**. Saved bindings block deletion (`STORAGE_BOUND`). Do not deploy yet: a deployment must bind every active resource.
4. In **Settings** > **Danger zone**, select **Delete**. Choose the resource under **Database or storage**.
5. Type the app's name, a space, and the storage type, for example `my-app postgres` (the types are `postgres`, `mongo`, and `s3`). Select **Delete permanently**.
6. Check **Overview** activity until it shows success. Check that the resource is gone from **Databases and storage**, then deploy the prepared commit with the saved settings.

### Edit storage limits (portal admins only)

Owners, teammates, staff, and admins can read usage and limits on **Settings** > **Databases and storage**. Only admins see **Edit limits**. Usage is a recorded measurement, with its update time; it may be stale or not measured yet. CPU shows a cap, not live CPU usage.

1. Select **Edit limits** beside the resource.
2. Enter the limits, then select **Save limits**. The portal submits the previous limits with the new values so a concurrent edit cannot be silently overwritten.
3. Wait for **Limits saved.** If the change fails, read its message before retrying.

| Limit | Allowed range | Effect |
| --- | --- | --- |
| PostgreSQL **Size target** | 1–500 GB | Reports database size; exceeding it does not pause writes. |
| MongoDB **Size limit** | 1–500 GB | Pauses writes above the limit. Reads and deletes continue; writes resume at or below 95%. |
| Database **Connection limit** | 1–100 | Caps app connections; changing it restarts that database instance. |
| Database **Memory limit (MB)** | 512–8,192, in whole MB | Hard memory cap, with no swap; changing it restarts that database instance. |
| Database **CPU limit (cores)** | 0.1–4, in increments of 0.001 | CPU quota for that database instance. |
| S3 **Size limit** | 1 MB–500 GB | Bucket byte quota. |
| S3 **Object limit** | 1–100,000,000 | Bucket object quota. |

The portal uses binary units: 1 GB = 1,024 MB and 1 MB = 1,048,576 bytes. Each database has its own server and hard disk allowance of 150% of its size target or limit, rounded up to a whole MB. The allowance includes database files and overhead. S3 uses a shared Garage service with a separate bucket and bucket-scoped key for each app.

MongoDB accepts the selected connection limit plus ten for overhead; the app's URI sets `maxPoolSize` to the limit. PostgreSQL sets its server connection maximum to the limit plus five reserved administrator connections. Keep application connection pools within the selected limit.

Lowering a database size needs fresh usage and room above that usage: 384 MB for PostgreSQL or 128 MB for MongoDB. The instance manager also checks the files on disk. Lowering a database size can briefly stop the instance while the disk allowance is checked. Host memory, connection, object, and disk budgets can refuse a change even within these ranges. Follow the portal's error message or ask an operator to add capacity.

If a resource shows **Memory after move** and **CPU after move**, it is still on a shared database server. Those caps apply after migration to its own instance. Ask an operator to migrate it before raising connection limits ([Migrate storage instances](migrate-storage-instances.md)).

### Allow an outage or change an app's size

On **Overview** > **Machines**, **Worker** shows the machine's vCPU count and total RAM, followed by the app's memory limit. If machine capacity is unavailable, it shows the flavor name and app memory limit. A flavor is an OpenStack VM size. Select **Change size** to open **Deploy** at **Worker size**.

Each app has a dedicated worker VM (virtual machine). Replacing it for a deploy, resize, or upgrade discards its local files. Fresh-worker deployments calculate the hard memory limit from measured capacity after leaving room for the system. The size review shows an upper bound until the worker starts. CPU is a soft scheduling share; the app can use all the worker's vCPUs (virtual CPUs).

Staff and portal admins have these deploy options:

- **Deployment method** > **Replace the running app in one step** chooses a maintenance cutover instead of starting the new version alongside it. The app goes offline briefly while the new version starts. For an app that keeps a fixed IP address, the portal applies this automatically and shows an information message about the brief outage.
- **Worker size** starts at **Keep current size**, with the current CPU and RAM. Choices are grouped by flavor family, with CPU then RAM increasing within each family. Choose a different size to see **Now**, **After deploy**, and how much memory the app can use. Review and deploy to apply the change. It replaces the worker; an app with a retained address uses maintenance after building. The portal sends the reviewed plan unchanged, and the controller checks it again before provisioning.

### Change the build machine

1. On **Overview** > **Machines**, find **Build machine**. It shows capacity and flavor, with **platform default** or **set for this app**.
2. Select **Change** to open **Settings** at the **Build machine** card.
3. Choose a size and select **Save**. To reset an app-specific choice, select **Use platform default**, then **Save**.
4. Wait for **Build machine saved.** Pending or failed changes show their activity.

The state says **Uses the platform default** or **Set for this app**. **Deploy** shows the effective build machine in its read-only **Settings** summary.

Each deploy builds the app on a temporary machine of this size. If a build runs out of memory, choose a machine with more RAM. A larger machine consumes more cloud quota while building. Build machines need at least 1 vCPU and 1 GB RAM.

Saving a build machine applies to builds that start afterwards. It keeps the worker, needs no sizing plan or maintenance, and does not change a build already started. Owners cannot read or change build machines.

Portal admins can open **Platform settings** near **People** and **Audit log**, change **Default build machine** in **Builds**, and select **Save**. **Default build machine saved.** confirms success and the select marks the current choice. The new default applies to subsequent builds of apps using **Platform default**; app-specific choices stay the same. Both app and default changes enter the audit log. The inventory's `flavors.builder` supplies the default until an admin sets one. Admin VM size remains an operator task.

## Help a person

Open **People** and search by name or username. Select the person to see their apps and recent activity, including changes needing attention. App links open the shared app pages.

Portal admins additionally see **Account controls** on that person’s page: role, limits, access, invitation and recovery actions. Staff do not see these controls and cannot use their API. **Create account** appears only for admins on **People**.

Account actions that change access ask for your password and a fresh authenticator code unless you confirmed them in the last five minutes. Changing limits does not require that confirmation. Codes cannot be reused; wait for a new code if you just signed in.

### Create a local account

1. Select **Create account**. Enter a **Username** (3 to 32 characters: lowercase letters, digits, `.`, `_`, or `-`, starting with a letter), an optional **Display name**, and a **Role**.
2. Select **Create account** and confirm.
3. Copy the **Setup link** and send it to the person privately. It works once and expires in 72 hours. The portal doesn't send email.

The person opens the link, sets a password, and, for a portal admin, adds an authenticator app. Owners and staff can choose **Use an authenticator app**. Until they finish, the account shows **Setup pending**, and **New invitation link** creates a fresh link.

### Make a Commons user staff

Have the person sign in through Commons once. Then open their account, set **Role** to **Staff**, and select **Change role**. Commons accounts can't be portal admins.

### Change a role, disable, or sign someone out

Open the account:

- **Change role** sets owner, staff, or portal admin. Making a local account a portal admin when it has no authenticator yet puts it back to **Setup pending** and gives you a link for them to add one.
- **Disable account** blocks sign-in; **Enable account** restores it. Disabling keeps the person's apps.
- **Sign out everywhere** ends all of the account's sessions.
- **Reset password** and **Reset authenticator** (local accounts only) clear the old credential at once and give you a link to send. The account can't sign in until the person finishes the link, even if the authenticator was optional. After an authenticator reset, they sign in again with their password and a new code. A password reset for an account with an authenticator asks for a current code, and the link stops working after five wrong codes.

Every one of these ends all of the account's sessions and cancels any links you sent it earlier. The new role takes effect the next time the person signs in. Resetting credentials doesn't enable a disabled account.

To replace both factors, send **Reset authenticator** first and wait for the person to finish it, then send **Reset password**.

### Set limits

Owners' default limits come from the inventory (`appLimit` and `concurrencyLimit`; see [Open the owner portal](open-the-portal.md#2-turn-on-the-portal-in-the-inventory)). To change one owner's, open their account and edit **Limits**:

| Field | Range | What it limits |
| --- | --- | --- |
| **Apps** | 0 to 1000 | How many apps the owner may have. Stopped apps count. |
| **Changes at a time** | 0 to 16 | How many foreground changes this account may start at once: deploys, storage, variables, starts, stops, and restarts. Teammate changes use the teammate's concurrency limit. Accepted deployment cleanup does not count. |

Select **Save limits**. Lowering a limit blocks new apps or changes once that limit is reached; setting it to 0 blocks all new ones. Existing apps and changes in progress remain. Apps a portal admin created for the owner count too.

Staff and portal admins have no limits, so their accounts show no **Limits** section. If you change a staff account back to owner, any limits you set earlier apply again.

<details>
<summary>When someone can't sign in with a local account</summary>

The portal shows the same credentials error for a wrong username, password, or code, and for the hourly account blocks. Short per-address blocks can instead show a wait message. The blocks it applies:

- Five failed attempts for one username from one address block that pair for the rest of its 60-second window, even with the right password.
- Twenty wrong passwords for one account within an hour, from anywhere, block password sign-in for that account until those failures are an hour old. A recognized browser has a separate allowance of 20 failures per hour. Its recognition cookie lasts up to 90 days; changing account access or credentials invalidates it.
- Wrong authenticator codes (after a correct password) start a 30-second wait that doubles with each failure, up to an hour, and allow at most ten failures an hour.

To help someone who is blocked, wait for the window to pass, or send **Reset password** or **Reset authenticator**. Restarting the broker clears the short per-address blocks but not the hourly password and code limits.

</details>

## Review the audit log

Open **Audit log** to see account changes and app-management actions, newest first, with the affected person, actor and time. Staff app changes and Resume actions appear here too. Passwords, authenticator codes, setup links and environment values are excluded.

The separate private read log records staff and admin reads and is retained for 30 days. It is available only through the portal database. If the service cannot record a read, it refuses that read. See [Internals](../reference/internals.md#staff-and-admin-authority) for authorization and read-log limits.

## Related

- [Open the owner portal](open-the-portal.md)
- [Deploy an app](for-app-owners.md)
- [Deploy apps from the command line](deploy-apps-from-the-command-line.md)
- [Backups and recovery](backups-and-recovery.md)
