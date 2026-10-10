# Migrate shared databases to isolated instances

This rollout moves existing platform PostgreSQL/MongoDB resources to individual
containers on an xl.16core storage VM (16 vCPU, 64 GiB), retaining the old databases.
It uses the existing pinned database major versions and controller schema migration 7.
The storage VM replacement causes the accepted short storage outage; the copy stage
then stops one app at a time. Copy time depends on bytes, indexes and volume throughput:
expect export + restore + verification + one application restart, usually seconds to
minutes for small resources. Measure the first app before estimating the rest.

1. Check that controller foreground/finishing operations are resolved, and take verified
   controller and managed-data backups. Check current PostgreSQL/MongoDB usage and the
   old directories' physical size. The temporary source project caps are 32 GiB each;
   increase the source reservations and rebuild if existing physical allocation exceeds
   those caps. Raise any resource's soft size before migration if its data plus 384 MiB (PostgreSQL WAL and overhead) or 128 MiB (MongoDB)
   exceeds the derived hard quota. This check happens before application quiescence.
2. Grow the data volume and XFS filesystem. The fifty-app default reservation is
   759.5 GiB, so use at least 1 TiB; the 85% warning boundary is then 870.4 GiB.
   The coordinator owns the Cinder resize and inventory update. Confirm the replacement
   flavor is xl.16core and the mounted filesystem is XFS with project quotas.
   The 100 GiB registry cap provides about 2 GiB per app across retained images;
   image bytes have no current policy maximum, so this is a bounded allowance,
   not a guarantee that fifty arbitrary image histories fit. Monitor and expand
   the registry reservation with the volume when image usage requires it.
3. Reconcile foundation security-group ingress: TCP 30000–30999 must be allowed to
   storage from the worker and admin groups. Replace the storage host with the new image, retaining shared databases. Verify the
   instance manager and host metrics services, nginx TLS, and both shared sources.
   The manager runs on loopback and authenticates the existing Garage admin bearer.
4. Install the matching helper release, replace the admin image, and deploy the portal
   broker/web pair from the same combined revision. The helper launcher uses its durable
   accepted release; replacing the admin image alone leaves the old helper installed.
   The portal pair consumes the new four-field database quota model and checks admin
   authorization on the project-socket limits route.
5. On the admin VM as agentops, run:

   ```sh
   openstack-platform-storage-migrate
   ```

   Save the printed request UUID. The privileged controller operation groups resources
   by app and records a child app reservation. Each app is quiesced once, its old logins
   frozen, and its databases copied and checked. Small tables/databases also receive
   checksums. Only a verified, sealed target is published. The controller resubmits the
   accepted Nomad job and checks scheduler health; users do not need to edit bindings.
   A stopped/never-deployed app stays stopped.
6. If the command times out or reports recovery_required, fix the named dependency or
   capacity issue and replay the same UUID:

   ```sh
   openstack-platform-storage-migrate --request-id UUID --timeout 1800
   ```

   The command timeout does not extend the controller/helper policy deadline. Set those
   policy bounds before rollout for unusually large copies. Replays skip switched
   resources and completed apps. An interrupted app remains quiesced until its replay
   finishes. Do not manually copy over a sealed target or clear the app reservation.
7. Establish and verify recurring managed-volume snapshots covering `instances/`,
   paired with controller/Nomad state backups. Existing shared-only logical exporters
   fail closed once the corresponding type has isolated instances; they must not be
   treated as covering new writes. Extend logical export/restore for isolated endpoints
   before removing the retained sources or relying on the old managed-data restore flow.
8. Verify `/v1/admin/storage` reports instance isolation, instance IDs/ports and switched
   migration state for every former shared resource. Verify each app's existing binding
   works, usage samples are fresh, and the operator dashboard shows container caps and
   no unexpected availability/connection alarms. On the storage host, check a unit's
   `systemctl show` MemoryMax, MemorySwapMax, CPUQuotaPerSecUSec, CPUWeight, IOWeight and
   TasksMax, and verify its XFS project quota. Run the final idempotent settings repair:

   ```sh
   openstack-platform-storage-repair
   ```

Old data is untouched by migration and is frozen against application writes. Before
endpoint publication, the source is the rollback authority. After publication the
instance may contain new writes, so switching back to the old source can lose data:
recover/replay the instance operation or use a verified backup instead.

A follow-up must remove the shared server containers, their frozen databases/users,
legacy ports and source quota reservations, and verified migration archives. It must
also complete instance-aware logical backup/restore and update monitoring to stop checking the shared listeners. This rollout deliberately
retains them; deleting an isolated resource deletes only its new instance data.

Order matters: new helpers require the new authenticated manager; new controller/API
and portal fields ship together; migration requires the new image and helper and
precedes final repair. An old helper rejects the new actions. An old storage image
has no manager or instance guardrails. A new portal against an old controller cannot
read/update the new quota shape. Repair before migration only repairs the old logins.
