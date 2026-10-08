# Manage apps and people

Use the owner portal to manage apps across the class and help people finish blocked changes. Staff and portal admins have the same app controls. Account controls and the audit log require a portal admin.

## Choose a destination

| Destination | URL | Who sees it | What it shows |
| --- | --- | --- | --- |
| **My apps** | `/apps` | Everyone | Apps you own or work on as a teammate |
| **All apps** | `/all-apps` | Staff and portal admins | Every managed app, with search by app or owner and a status filter |
| **People** | `/people` | Staff and portal admins | Everyone, including owners, staff, admins and accounts awaiting setup; each person’s apps and activity |
| **Activity** | `/activity` | Staff and portal admins | Class-wide changes, with blocked and unknown changes at the top |
| **Audit log** | `/audit` | Portal admins | Account changes and app-management actions |

A portal admin lands on **People** after signing in. Other accounts land on **My apps**. There are no role-named navigation sections. Retired portal and broker URLs are removed; update bookmarks to these destinations.

## Open and manage an app

Open **All apps**, search by app name, owner name or username, and select the app. Use **Status** to filter the full catalog, including **Needs attention**. An app appears after it is created in the portal or adopted. Lists contain 25 apps per page. **Status** shows blocked or unknown changes directly below the app state only when there is a change to attend to. Hover over the app state to see when it was last checked. The list refreshes automatically; observations older than ten minutes show **Unknown** while a new check runs.

Staff and portal admins open the same app pages as owners without joining the team:

- **Overview** shows the current deployment, activity and start, stop and restart controls. Staff and admins also see **Machines**, with the worker and effective build machine.
- **Settings** holds repository and runtime settings, deploy keys, environment variables, databases and storage. Staff and admins additionally get **Danger zone**, with **Change owner** and storage deletion.
- **Deploy** lets you choose a commit, review it, and deploy. Staff and admins additionally get the deployment method, worker size, and a read-only build machine summary.
- **Deployments** lists attempts. Open one to see its details and build output.
- **Logs** shows runtime output and errors.
- **Team** lets owners, staff and admins add or remove teammates. Members can leave their own team.

The optional **Build script** has no default. Leave it empty when the app has no build step. Environment values stay write-only for everyone. See [Deploy an app](for-app-owners.md) for the shared controls.

Staff and admins have no app or concurrency limit. Creating an app for another person uses that owner’s app limit. Every app still accepts one change at a time. Staff and admin app changes appear under their usernames in **Audit log**; their reads go into a separate private read log.

## Resume a change that needs attention

**Needs attention** means a change is blocked; **Unknown** means its outcome has not been confirmed. Both offer **Resume** when the change can be replayed. Find the same status and action on **Overview**, deployment lists and details, **All apps**, **Activity**, or the person’s apps and activity under **People**.

Select **Resume** to continue the original request with its original commit, settings and request keys. Wait for it to finish before starting another change. Staff and admins can resume anyone’s app change without a password or authenticator confirmation; owners and teammates can resume changes on their apps. The audit log records who resumed it.

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

### Delete a database or storage

> [!WARNING]
> Deleting storage destroys the database or bucket and all its data, even if the bucket isn't empty. Recovery requires the operator and a managed-data backup; changes since the backup are lost.

1. Under **Databases and storage** in the app's settings, open **Edit variables** for the resource. Remove every variable, select **Save variables**, and deploy so the running app stops using it. Saved settings that still connect to the resource block deletion (`STORAGE_BOUND`).
2. Make sure a recent managed-data backup exists ([Backups and recovery](backups-and-recovery.md)).
3. In **Settings** > **Danger zone**, select **Delete**. Choose it under **Database or storage**.
4. Type the app's name, a space, and the storage type, for example `my-app postgres` (the types are `postgres`, `mongo`, and `s3`). Select **Delete permanently**.
5. Check the app’s **Overview** activity until it shows success, then check that the storage is gone from **Databases and storage**.

### Allow an outage or change an app's size

On **Overview** > **Machines**, **Worker** shows the machine's vCPU count and total RAM, followed by the memory available to the app after the system reserve and approximate speed per vCPU. If machine capacity is unavailable, it shows the flavor name, app memory allowance and CPU speed explicitly labelled as a total. Select **Change size** to open **Deploy** at **Worker size**.

Staff and portal admins have these deploy options:

- **Deployment method** > **Replace the running app in one step** chooses a maintenance cutover instead of starting the new version alongside it. The app goes offline briefly while the new version starts. For an app that keeps a fixed IP address, the portal applies this automatically and shows an information message about the brief outage.
- **Worker size** starts at **Keep current size**, with the current CPU and RAM. Choices are grouped by flavor family, with CPU then RAM increasing within each family. Choose a different size to see **Now**, **After deploy**, and how much memory the app can use. Review and deploy to apply the change. It replaces the worker; an app with a retained address uses maintenance after building. The portal sends the reviewed plan unchanged, and the controller checks it again before provisioning.

### Change the build machine

On **Overview** > **Machines**, **Build machine** shows its capacity and flavor, with **platform default** or **set for this app**. Select **Change** to open the app's **Settings** page at the **Build machine** card. Choose a size and select **Save**. The state says **Uses the platform default** or **Set for this app**. To reset an app-specific choice, select **Use platform default**, then **Save**. **Build machine saved.** confirms success; pending or failed changes show their activity. The **Deploy** page shows the effective build machine in its read-only **Settings** summary.

Each deploy builds the app on a temporary machine of this size. A bigger machine builds faster and handles heavy builds such as Next.js; a smaller one lets more builds run at once. If a build runs out of memory, choose a bigger machine. Build machines need at least 1 vCPU and 1 GB RAM.

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

## Review the audit log

Open **Audit log** to see account changes and app-management actions, newest first, with the affected person, actor and time. Staff app changes and Resume actions appear here too. Passwords, authenticator codes, setup links and environment values are excluded.

The separate private read log records staff and admin reads and is retained for 30 days. It is available only through the portal database. If the service cannot record a read, it refuses that read. See [Internals](../reference/internals.md#staff-and-admin-authority) for authorization and read-log limits.

## Related

- [Open the owner portal](open-the-portal.md)
- [Deploy an app](for-app-owners.md)
- [Deploy apps from the command line](deploy-apps-from-the-command-line.md)
- [Backups and recovery](backups-and-recovery.md)
