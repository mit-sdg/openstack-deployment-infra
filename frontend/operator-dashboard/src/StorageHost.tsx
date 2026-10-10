import { LegacyCard as Card } from "@openstack-platform/ui";
import type { StorageHost as Host } from "./snapshot";
import { formatBytes, numberFormat, Time } from "./presentation";

function containerLabel(name: string) {
  const products: Record<string, string> = {
    postgres: "PostgreSQL",
    postgresql: "PostgreSQL",
    mongo: "MongoDB",
    mongodb: "MongoDB",
    garage: "Garage",
    registry: "Registry",
  };
  const product = name.match(
    /(?:^|-)(postgres|postgresql|mongo|mongodb|garage|registry)(?:-[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12})?$/,
  )?.[1];
  return product ? products[product] : name;
}
function bytesOf(used: number, total: number) {
  const formatted = formatBytes(total);
  const [amount, unit] = formatted.split(" ");
  const scale = 1024 ** ["B", "KiB", "MiB", "GiB", "TiB"].indexOf(unit);
  return `${numberFormat.format(Math.round((used / scale) * 10) / 10)} of ${amount} ${unit}`;
}
function Meter({
  label,
  used,
  total,
  text,
  load = false,
}: {
  label: string;
  used: number;
  total: number;
  text: string;
  load?: boolean;
}) {
  const ratio = total > 0 ? used / total : 0;
  const tone =
    ratio >= (load ? 1.5 : 0.95)
      ? "critical"
      : ratio >= (load ? 1 : 0.8)
        ? "warning"
        : "good";
  const state =
    tone === "critical"
      ? load
        ? "High load"
        : "Nearly full"
      : tone === "warning"
        ? load
          ? "Elevated load"
          : "High usage"
        : null;
  return (
    <div className="storage-host__metric" data-tone={tone}>
      <dt>{label}</dt>
      <dd>
        <div className="storage-host__value">
          <span>{text}</span>
          {state && <span className="storage-host__state">{state}</span>}
        </div>
        {total > 0 && (
          <progress
            className="storage-host__meter"
            aria-label={label}
            aria-valuetext={text}
            value={Math.min(used, total)}
            max={total}
          />
        )}
      </dd>
    </div>
  );
}
export function StorageHost({ host }: { host: Host | null }) {
  return (
    <section className="section" aria-labelledby="storage-host-title">
      <Card className="storage-host">
        <div className="card__header">
          <h2 className="card__title" id="storage-host-title">
            Storage host
          </h2>
          {host?.stale && (
            <span className="chip storage-host__stale" data-tone="warning">
              Stale
            </span>
          )}
          {host && (
            <span className="card__note">
              <Time value={host.measuredAt} prefix="Updated " />
            </span>
          )}
        </div>
        <div className="storage-host__body">
          {host ? (
            <>
              <dl className="storage-host__metrics">
                <Meter
                  label="Load (1 minute)"
                  used={host.loadAverage[0]}
                  total={host.cpuCount}
                  text={`${host.loadAverage[0].toFixed(2)} of ${host.cpuCount} CPUs`}
                  load
                />
                <Meter
                  label="Memory"
                  used={Math.max(
                    0,
                    host.memory.totalBytes - host.memory.availableBytes,
                  )}
                  total={host.memory.totalBytes}
                  text={bytesOf(
                    Math.max(
                      0,
                      host.memory.totalBytes - host.memory.availableBytes,
                    ),
                    host.memory.totalBytes,
                  )}
                />
                <Meter
                  label="Data volume"
                  used={host.dataVolume.usedBytes}
                  total={host.dataVolume.totalBytes}
                  text={bytesOf(
                    host.dataVolume.usedBytes,
                    host.dataVolume.totalBytes,
                  )}
                />
                {host.containers.map((container) => (
                  <Meter
                    key={container.name}
                    label={`${containerLabel(container.name)} memory`}
                    used={container.usedBytes}
                    total={container.limitBytes}
                    text={
                      container.limitBytes > 0
                        ? bytesOf(container.usedBytes, container.limitBytes)
                        : `${formatBytes(container.usedBytes)} · No memory limit reported`
                    }
                  />
                ))}
                <Meter
                  label="PostgreSQL connections"
                  used={host.postgresConnections.current}
                  total={host.postgresConnections.limit}
                  text={`${numberFormat.format(host.postgresConnections.current)} of ${numberFormat.format(host.postgresConnections.limit)}`}
                />
                <Meter
                  label="MongoDB connections"
                  used={host.mongoConnections.current}
                  total={host.mongoConnections.limit}
                  text={`${numberFormat.format(host.mongoConnections.current)} of ${numberFormat.format(host.mongoConnections.limit)}`}
                />
              </dl>
              <p className="storage-host__history muted">
                Load over 5 / 15 minutes:{" "}
                {host.loadAverage
                  .slice(1)
                  .map((value) => value.toFixed(2))
                  .join(" / ")}
                {host.stale && " · Showing the last measured values"}
              </p>
            </>
          ) : (
            <p className="muted">Storage host usage not measured yet.</p>
          )}
        </div>
      </Card>
    </section>
  );
}
