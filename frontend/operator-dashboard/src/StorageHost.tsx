import { LegacyCard as Card } from "@openstack-platform/ui";
import type { StorageHost as Host } from "./snapshot";
import { formatBytes, Time } from "./presentation";

export function StorageHost({ host }: { host: Host | null }) {
  return (
    <section className="section" aria-labelledby="storage-host-title">
      <Card className="storage-host">
        <div className="card__header">
          <h2 className="card__title" id="storage-host-title">
            Storage host
          </h2>
        </div>
        <div className="storage-host__body">
          {host ? (
            <>
              <p className="muted">
                <Time value={host.measuredAt} prefix="Updated " />
                {host.stale && " · Stale observation"}
              </p>
              <dl className="storage-host__metrics">
                <div>
                  <dt>Load (1 / 5 / 15 minutes)</dt>
                  <dd>
                    {host.loadAverage
                      .map((value) => value.toFixed(2))
                      .join(" / ")}{" "}
                    · {host.cpuCount} CPUs
                  </dd>
                </div>
                <div>
                  <dt>Memory</dt>
                  <dd>
                    {formatBytes(
                      Math.max(
                        0,
                        host.memory.totalBytes - host.memory.availableBytes,
                      ),
                    )}{" "}
                    of {formatBytes(host.memory.totalBytes)}
                  </dd>
                </div>
                <div>
                  <dt>Data volume</dt>
                  <dd>
                    {formatBytes(host.dataVolume.usedBytes)} of{" "}
                    {formatBytes(host.dataVolume.totalBytes)}
                  </dd>
                </div>
                {host.containers.map((container) => (
                  <div key={container.name}>
                    <dt>{container.name} memory</dt>
                    <dd>
                      {formatBytes(container.usedBytes)} of{" "}
                      {formatBytes(container.limitBytes)}
                    </dd>
                  </div>
                ))}
                <div>
                  <dt>PostgreSQL connections</dt>
                  <dd>
                    {host.postgresConnections.current} of{" "}
                    {host.postgresConnections.limit}
                  </dd>
                </div>
                <div>
                  <dt>MongoDB connections</dt>
                  <dd>
                    {host.mongoConnections.current} of{" "}
                    {host.mongoConnections.limit}
                  </dd>
                </div>
              </dl>
            </>
          ) : (
            <p className="muted">Storage host usage not measured yet.</p>
          )}
        </div>
      </Card>
    </section>
  );
}
