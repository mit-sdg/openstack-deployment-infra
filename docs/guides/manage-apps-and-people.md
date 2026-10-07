# Manage apps and people

Use this guide to help students with their apps and, as a portal admin, manage accounts, limits, and audit records. Install the portal first with [Open the owner portal](open-the-portal.md).

## Roles

Every account has one role. Sessions keep the role assigned at sign-in.

| What you can do | Owner | Staff | Portal admin |
| --- | --- | --- | --- |
| Create your own apps | Up to your limit | No limit | No limit |
| Manage your own and teammates' apps: settings, variables, add/verify/rotate storage, deploy, stop/start/restart, logs, deploy keys | Yes | Yes | Yes |
| Add and remove teammates | On apps you own | On any app | On any app |
| Read the course catalog (**Staff** pages) | No | Yes | Yes |
| Manage any app: settings, variables, storage, deploy, stop, start, logs, team | No | Yes | Yes |
| Create an app for someone else | No | Yes | Yes |
| Adopt an app the operator deployed, or change an app's owner | No | Yes | Yes |
| Delete a database or storage | No | Yes, with typed confirmation | Yes, with typed confirmation |
| Allow a brief outage or set an app's size when deploying | No | Yes | Yes |
| Manage accounts, roles, and limits; read the audit log | No | No | Yes |
| Sign in with | Commons or a local account | Commons or a local account | A local account with an authenticator app |

App actions need no password or authenticator confirmation for staff or portal admins. Account changes still ask for both in **Confirm it’s you**, unless you confirmed them in the last five minutes.

Where to find things:

- Everyone has **Apps**, which lists only the apps you own or are a teammate on.
- Staff also have **Staff** (the course catalog, with **Owners** and **Activity** tabs) and **Manage apps** (every app).
- Portal admins have **Staff** and **Admin**, with the tabs **All apps**, **Accounts**, and **Audit log**. A portal admin lands on **Accounts** after signing in.

## Manage any app

Staff open **Manage apps**; portal admins open **Admin** > **All apps**. The list shows every app the portal knows about, 25 to a page, with its status, owner, URL, last deployment, and app ID. Apps that the operator created outside the portal don't appear until staff or a portal admin adopts them ([Adopt an app](#adopt-an-app-the-operator-deployed)).

Select an app for its management tools:

- **Deploy** opens a dialog with the branch's recent commits. Pick one or paste the full 40-character SHA. The portal checks the commit against the build's rules before you deploy.
- **Stop app** and **Start app** change whether the app runs. Stopping takes it offline and keeps its settings and data.
- **Details** shows the owner, URL, last deployed commit, current size, and app ID.
- **Logs** shows the last 500 lines of runtime output or errors. Find build output under **Deployments**.
- The settings form, **Private repository**, **Environment variables**, and **Databases and storage** work as described in [Deploy an app](for-app-owners.md). Environment values stay write-only for everyone: no one, including portal admins, can read a value back.
- **Team** lets you add or remove teammates.

Changes appear in the **Audit log** under your username. Staff and portal admins have no concurrency limit, and your changes don't use the owner's limit. Each app still accepts only one change at a time.

## Create, adopt, and move apps

Staff and portal admins can create apps for others, transfer ownership, delete storage, and change app sizing.

### Create an app for someone

On **All apps**, select **Create app**, enter the **App name**, choose the **Owner**, and select **Create app**. The app counts against that owner's limit, and the owner must be an active, enabled account.

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

On the app's page, under **Danger zone**, select **Change owner**, choose the new owner, and select **Change owner**. The portal refuses while the app has a change in progress (`APP_BUSY`), or if someone else changed the owner since you loaded the page (`OWNER_CONFLICT`; reload). If the new owner was a teammate, they're removed from the team list; other teammates stay. The new owner keeps the app even if it puts them over their limit.

### Delete a database or storage

> [!WARNING]
> Deleting storage destroys the database or bucket and all its data, even if the bucket isn't empty. Recovery requires the operator and a managed-data backup; changes since the backup are lost.

1. Under **Databases and storage** in the app's settings, open **Edit variables** for the resource. Remove every variable, select **Save variables**, and deploy so the running app stops using it. Saved settings that still connect to the resource block deletion (`STORAGE_BOUND`).
2. Make sure a recent managed-data backup exists ([Backups and recovery](backups-and-recovery.md)).
3. Under **Danger zone**, select **Delete a database or storage**. Choose it under **Database or storage**.
4. Type the app's name, a space, and the storage type, for example `my-app postgres` (the types are `postgres`, `mongo`, and `s3`). Select **Delete permanently**.
5. Watch **Latest change** until it shows success, then check that the storage is gone from **Databases and storage**.

### Allow an outage or change an app's size

Staff and portal admins have these deploy options:

- **Deployment method** > **Replace the running app in one step** chooses a maintenance cutover instead of starting the new version alongside it. The app goes offline briefly while the new version starts. For an app that keeps a fixed IP address, the portal applies this automatically and shows an information message about the brief outage.
- **Sizing plan** takes a reviewed plan, as JSON, from the operator. Leave it empty to keep the app's current size. The operator creates plans with the controller API ([Deploy apps from the command line](deploy-apps-from-the-command-line.md)).

## The Commons app

The portal recognizes Commons when an app's public URL matches `commonsOrigin`. App controls show **Signing in to this portal depends on this app.** as information. Deploying, stopping, starting, restarting, changing its owner, and storage actions proceed without a confirmation checkbox. Adoption also explains the sign-in dependency.

If Commons goes down, new Commons sign-ins fail. Existing portal sessions and local sign-in keep working. Keep a local portal admin available for repairs.

## Manage accounts

**Admin** > **Accounts** lists Commons and local accounts. Search by name or username. Rows show status (**Active**, **Setup pending**, or **Disabled**), username, role, sign-in method, app count and limit, and last sign-in. Select a row to manage access.

Actions that change who can do what ask you to confirm with your password and a fresh code: creating an account, changing a role, enabling or disabling, **Sign out everywhere**, and reset or invitation links. Changing limits doesn't.

> [!NOTE]
> Authenticator codes can't be reused. If the portal rejects a code you used a moment ago, for example right after signing in, wait for your app to show the next one.

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
| **Changes at a time** | 0 to 16 | How many changes (deploys, storage, variables, starts, stops, and restarts) the owner may have in progress at once. |

Select **Save limits**. Lowering a limit blocks new apps or changes once that limit is reached; setting it to 0 blocks all new ones. Existing apps and changes in progress remain. Apps a portal admin created for the owner count too.

Staff and portal admins have no limits, so their accounts show no **Limits** section. If you change a staff account back to owner, any limits you set earlier apply again.

<details>
<summary>When someone can't sign in with a local account</summary>

The portal shows the same message for a wrong username, password, or code, and for a temporary block, so it doesn't reveal which accounts exist. The blocks it applies:

- Five failed attempts for one username from one address block that pair for a minute, even with the right password.
- Twenty wrong passwords for one account within an hour, from anywhere, block password sign-in for that account until those failures are an hour old. A browser where the person signed in successfully in the last 90 days has its own allowance, so they can usually still sign in from their own computer.
- Wrong authenticator codes (after a correct password) start a 30-second wait that doubles with each failure, up to an hour, and allow at most ten failures an hour.

To help someone who is blocked, wait for the window to pass, or send **Reset password** or **Reset authenticator**. Restarting the broker clears the short per-address blocks but not the hourly password and code limits.

</details>

## Read the course catalog

The **Staff** pages give staff and portal admins a read-only view of the class:

- **Owners** lists accounts with the Owner role. Select one to see their app count and limit, apps, and recent activity.
- **Activity** lists every change across the class, newest first, and filters by owner or app.

These pages show metadata only: names, app URLs, repository addresses, deployment status, and commits. They never show environment variables, storage, or logs; an app's management page shows its variable names, storage, and runtime logs. Lists show 25 entries a page.

The portal records each catalog read in a private read log for 30 days. If the portal is too busy, or can't write that record, the page shows an error; wait 30 seconds before trying again.

## Review the audit log

**Admin** > **Audit log** lists account changes and every change made through app management, newest first, with the action, the account it affected, who did it, details such as the app or the new role, and when. It never contains passwords, codes, links, or environment values. Changes staff make to other people's apps appear here too.

What staff and portal admins look at (catalog pages and app management pages) goes to the separate private read log, not the audit log. Only someone with access to the portal's database can read it.

<details>
<summary>Recover read-log capacity</summary>

Pruning happens during staff reads, in batches of up to 1,000 expired entries. If a full batch is removed, the next read can prune another; once fewer than 1,000 remain to delete, pruning waits a day. An unused portal can keep entries beyond 30 days. At 2 million entries, or if a read can't be recorded, staff catalog and app management reads fail. Owners aren't affected.

If that happens:

1. Use the operator's recovery connection to stop the portal services and backup timer, then take a broker backup ([Backups and recovery](backups-and-recovery.md)).
2. Export the private read-log entries to your approved private evidence store.
3. As the `management-broker` account, in one SQLite transaction on `<adminState>/management-broker/management.sqlite3`: delete the reviewed or expired rows from `staff_read_audit`, set `staff_read_state.row_count` to the result of `SELECT COUNT(*) FROM staff_read_audit`, and set `staff_read_state.pruned_at` to the current Unix timestamp.
4. Check the database's integrity and the counts, take a new backup, and start the services again.

Never reset the counter without removing the rows, and never delete recent entries only to let more staff reads through.

</details>

## Related

- [Open the owner portal](open-the-portal.md): installing the portal, the first portal admin, and recovering a lost one
- [Roll back to a retained portal pair](open-the-portal.md#roll-back-to-a-retained-portal-pair): returning to an earlier portal release
- [Deploy an app](for-app-owners.md): what owners see, and the guide to share with students
- [Deploy apps from the command line](deploy-apps-from-the-command-line.md): operator deployments, sizing plans, and app IDs
- [Backups and recovery](backups-and-recovery.md): managed-data backups and restoring the portal's database
- [Security](../security.md#the-owner-portal): why the roles are split this way
