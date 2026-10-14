# Deploy an app (for app owners)

Use 6.1040 Apps to put your Node.js or Bun web app online from a GitHub repository. This guide is for app owners and teammates. Course staff will give you the portal address; these examples use `example.edu`.

## Sign in

1. Open the portal (for example `https://example.edu`) and select **Sign in with your class account**. Your class may use a different label on this button.
2. Approve the portal on your class site. You return to **My apps**.

If course staff gave you a local account, finish its setup link, then select **Use a local account** to sign in.

You stay signed in for up to 8 hours, or until you've been inactive for 30 minutes (your class may use different times).

## Get your repository ready

The platform installs from your lockfile and runs your package scripts. It doesn't use Dockerfiles or custom build commands. [The glossary](../README.md#glossary) explains the platform terms and binary size units. Before deploying, check that:

- `package.json` is a regular file in the repository root, at most 64 KiB, with your start script and any build script you use.
- Each package directory has a committed regular lockfile of at most 1 MiB: `package-lock.json` for Node.js, or `bun.lock` or `bun.lockb` for Bun.
- Your server listens on the port in the `PORT` environment variable, on all interfaces.
- A small health endpoint, such as `/health`, answers with HTTP 2xx and a body of at most 4 KiB (4,096 bytes), without redirects. A full HTML page is often too big.
- Persistent data lives in a database or bucket. Each app has its own worker, a virtual machine that runs the app. Portal deployments replace this worker and discard its local files.

For example, with Express:

```js
app.get('/health', (req, res) => res.send('ok'));
app.listen(Number(process.env.PORT ?? 3000), '0.0.0.0');
```

## Create an app

1. On **My apps**, select **Create app**.
2. Enter an **App name**: 3 to 40 lowercase letters, numbers, and single hyphens, starting with a letter and ending with a letter or number. Some names are reserved; deleted app names can't be reused.
3. Select **Create app**. The portal opens **Settings**.

The name becomes your address, `https://<app-name>.example.edu`, and can't be changed later.

**My apps** shows how many of your allowed apps you've used, for example `1 of 2`. Ask course staff if you need more, or if you want an app deleted.

## Fill in the settings

On **Settings**, fill in these fields:

| Setting | Default | What to enter |
| --- | --- | --- |
| **Repository URL** | none | `https://github.com/<owner>/<repository>` |
| **Branch** | `main` | The branch whose recent commits the portal lists. You always deploy an exact commit. |
| **Runtime** | Node.js | **Node.js** (installs with `npm ci`) or **Bun** (installs with `bun install --frozen-lockfile`) |
| **Package directories** | `.` | One per line; dependencies are installed in each. `.` is the repository root. |
| **Build script** | none | A script in the root `package.json` to run after installing, such as `build` |
| **Start script** | `start` | The script that starts your server |
| **Port** | `3000` | The port your server listens on. `PORT` is set to this value. |
| **Health check path** | `/health` | The path checked before visitors see a new version |

Select **Save settings**. Settings take effect on your next deploy.

**Private repository?** Under **Private repository**, select **Create deploy key** and copy the key. On GitHub, open the repository's **Settings** > **Deploy keys** > **Add deploy key**, paste it, and leave **Allow write access** off. Back in the portal, **Check access** should report that GitHub accepts the key. The key can only read that one repository.

## Add environment variables

Under **Environment variables**, enter a **Variable name** (`A`-`Z`, `0`-`9`, and `_`, starting with a letter) and a **New value**, then select **Save variable**.

Saved values are hidden from everyone, including you. To change one, save a new value under the same name. Saving restarts a running app without a deploy. Variables are available when your app runs, not during builds.

The platform reserves `PORT`, `NODE_ENV`, `PLATFORM_ENV`, `PLATFORM_PROJECT_ID`, `PLATFORM_PROJECT_SLUG`, `NODE_EXTRA_CA_CERTS`, `AWS_REQUEST_CHECKSUM_CALCULATION`, and names starting with `STORAGE__`. It sets `HOST=0.0.0.0` and `NODE_ENV=PLATFORM_ENV=production`.

## Add a database or file storage

Through the portal, you can add one PostgreSQL database, one MongoDB database, and one S3 bucket per app.

1. Under **Databases and storage**, select **Add PostgreSQL**, **Add MongoDB**, or **Add S3 storage**, and confirm. Only staff or a portal admin can delete it later, so add only what you need.
2. When it's ready, select **Use default variables**, or **Choose names** to pick your own.
3. Deploy, so your app receives the variables.

| Storage | Default variables |
| --- | --- |
| PostgreSQL | `DATABASE_URL` (complete, with password and TLS settings), `PGHOST`, `PGPORT`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`, `PGSSLMODE`, `PGSSLROOTCERT` |
| MongoDB | `MONGODB_URI` |
| S3 | `S3_ENDPOINT`, `S3_PUBLIC_ENDPOINT`, `S3_REGION`, `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_BUCKET` |

Saved variable names stay as you chose them. **Reset to defaults** in the variables dialog replaces them with the names in this table when you save.

Connections use TLS, and your app already trusts the platform certificates. **Verify** checks that the storage works. **Rotate credentials** replaces the credentials and restarts a running app to check them. Databases are backed up every night.

### Read storage usage and limits

**Databases and storage** shows the last measured usage, the limits, and when the measurement was taken. **Usage not measured yet** and **Usage may be out of date** mean the numbers are unavailable or stale. Refreshing the page does not guarantee a new measurement.

| Resource | What the limits mean |
| --- | --- |
| PostgreSQL | **Size target** is for reporting; exceeding it does not pause writes. The separate **Disk allowance** caps database files and overhead. Connections, memory, and CPU are capped for this database. |
| MongoDB | **Size limit** pauses writes when usage exceeds it. Reads and deletes still work. Delete enough data to leave at least 5% free; writes resume automatically within a few minutes. Connections, memory, CPU, and disk are also capped. |
| S3 storage | **Size limit** and **Objects** cap the bucket's stored bytes and object count. |

Each database runs on its own database server. S3 uses a shared service with a separate bucket and a key limited to that bucket. Only a portal admin can select **Edit limits**; ask course staff to arrange a higher limit.

<details>
<summary>Using S3 from your app</summary>

Use `S3_ENDPOINT` for server requests and `S3_PUBLIC_ENDPOINT` (`https://s3.example.edu`) for signed browser upload or download links. [Bun's S3 client](https://bun.sh/docs/runtime/s3#credentials) reads `S3_ACCESS_KEY_ID`, `S3_SECRET_ACCESS_KEY`, `S3_REGION`, `S3_ENDPOINT`, and `S3_BUCKET` automatically, with AWS variable fallbacks. AWS SDK clients need the endpoint, region, and credentials passed explicitly from these variables; pass the bucket to each request.

For browser links, the AWS SDK client needs `forcePathStyle: true`. The platform handles CORS (permission for browser requests from your app's pages); no bucket CORS setup is needed.

```js
// Bun
const url = Bun.s3.presign(key, {
  method: "PUT", expiresIn: 600, endpoint: process.env.S3_PUBLIC_ENDPOINT
});
// AWS SDK: a hostname endpoint needs path-style addressing
import { S3Client } from "@aws-sdk/client-s3";
const signer = new S3Client({
  endpoint: process.env.S3_PUBLIC_ENDPOINT,
  region: process.env.S3_REGION,
  credentials: {
    accessKeyId: process.env.S3_ACCESS_KEY_ID,
    secretAccessKey: process.env.S3_SECRET_ACCESS_KEY
  },
  forcePathStyle: true
});
```

File keys used through the public endpoint can't contain `//` or `..`. Browser uploads have your class's web request size limit; split larger files into multipart uploads.

</details>

## Deploy

1. Open **Deploy**. Pick one of the newest commits on your branch, or paste a full 40-character lowercase commit SHA (the commit's Git identifier).
2. The portal checks the commit against the build rules. If it says **This commit will fail to build**, fix what it lists first.
3. Select **Review deployment**, check the summary, and select **Deploy**.
4. Watch **Deployment progress**. A deploy usually takes several minutes and ends with **Deployment succeeded. Your app is running this commit.**

**Deploy latest** on **Overview** picks the newest commit on your branch and opens the same review. Every deploy uses your current saved settings and variables.

## Watch and control your app

- On **Overview**, check status. **Stop app** removes the worker but keeps storage. **Start app** starts the accepted deployment on a new worker. **Restart app** restarts the container on its current worker.
- Under **Deployments**, open a deploy for **Build output** or **Why it stopped**. **Deploy this commit again** uses your current settings.
- **Logs** shows up to the last 500 lines of **Output** or **Errors** from your running app.
- Under **Team**, **Add by username** gives up to 10 teammates access to settings, deployments, variables, storage, and logs. Only the owner, staff, or an admin can add or remove other teammates; a teammate can leave. Teammates need an active, enabled account. Ask class-account users to sign in once first. The app counts only against its owner's app limit. Changes you start count against your own concurrency limit (changes in progress at once).

New deployments prefix each output and error line with a UTC timestamp. **Logs**, **Build output**, and **Why it stopped** show these times in your local time, with the UTC value on hover. Use **Show timestamps** to hide the time column; **Copy logs** and **Download logs** keep the UTC timestamps. Apps that print their own timestamps show both. Older images keep their original output until you deploy again; restarting or rolling back to an older image does not add timestamps.

## Choose a Node.js or Bun version

To choose a version, add one of these to your repository. The first one found wins; otherwise, builds use the platform default:

| Runtime | Where | Examples |
| --- | --- | --- |
| Node.js | `engines.node` in the root `package.json`, then a root `.nvmrc`, then `.node-version` | `">=22 <23"`, `"22.x"`, `v22.11.0`, `lts/*` |
| Bun | `"packageManager"` in the root `package.json` (exact version only), then `engines.bun`, then a root `.bun-version` | `"bun@1.4.0"`, `">=1.2"` |

Builds choose the newest matching official release, preferring long-term support (LTS) for Node.js ranges. Ranges can change between builds; exact versions stay fixed. The deployment page shows the version used. Node.js before 20 and Bun before 1.1 aren't supported. Commit checks catch invalid requests; builds also fail if no official release matches.

## When a deploy fails

If an ordinary deployment fails before acceptance, your previous version keeps running. A deployment that staff configured to replace the running app in one step can leave the app stopped after cutover; ask staff to restore it. Under **Deployments**, open the failed deploy for **Build output** (the last 200 lines), **Why it stopped**, and suggested fixes:

| Problem | What to do |
| --- | --- |
| The build couldn't use this commit | Check that the commit is pushed, `package.json` is in the root, each package directory has its lockfile, and your scripts are in the root `package.json`. |
| Your app didn't become healthy in time | Listen on `PORT` on all interfaces, and make the health check path return HTTP 2xx with at most 4 KB. |
| A private repository fails to build | Select **Check access** under **Private repository** and fix the deploy key on GitHub. |
| The platform is busy, or couldn't look up a version | Try again in a few minutes. |
| The app can't reach its database | Select **Use default variables** (or **Choose names**), then deploy. |
| The commit list doesn't load | GitHub limits how often it can be asked. Paste the commit SHA instead. |
| **Your change to … didn't finish** | Select **Finish change**, enter the same value again, and save. |
| **Needs attention** | Open your app's **Overview** or **Deployments**. In **Needs attention**, select **Resume** on the unfinished deployment. Ask course staff if it stays blocked. |

A deploy that needs attention has unfinished work, even if the new version is online. Before acceptance, that work blocks another deploy or environment edit. After acceptance, cleanup retries automatically and compatible changes can proceed.

**Resume** continues the same deploy with its original commit and settings. It doesn't create another deployment. You or a teammate can resume ordinary app changes. Changes started through staff controls may need staff to resume them; only an admin can resume storage-limit changes or whole-app deletion.

## Related

- [How it works](../how-it-works.md): what happens behind the scenes when you deploy
- [Manage apps and people](manage-apps-and-people.md): what course staff can do for you
