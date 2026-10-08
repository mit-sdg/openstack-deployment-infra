import { fields, intentData, record, request } from './api';

export type Flavor = {
  flavor_id: string;
  name: string;
  vcpus: number;
  ram_mib: number;
  disk_gib: number;
};
export type ResizePlan = {
  applicationId: string;
  deploymentId: string | null;
  activation: string;
  current: { enabled: boolean; flavor: string; cpuMHz: number; memoryMiB: number };
  flavor: Flavor;
  allocation: string;
  reserve: { cpuMHzMinimum: number; memoryMiBMinimum: number; percentMinimum: number };
  fingerprint: string;
};
export type BuilderSize = { flavor: Flavor; defaultFlavor: Flavor; useDefault: boolean };
const flavor = (v: unknown) =>
  fields(v, {
    flavor_id: 'string',
    name: 'string',
    vcpus: 'number',
    ram_mib: 'number',
    disk_gib: 'number',
  }) as Flavor;
const flavors = (v: unknown) => {
  if (!Array.isArray(v)) throw new Error('Invalid service response');
  return v
    .map(flavor)
    .sort((a, b) => a.vcpus - b.vcpus || a.ram_mib - b.ram_mib || a.name.localeCompare(b.name));
};
export const sizeLabel = (size: Flavor) =>
  `${size.vcpus} vCPU · ${size.ram_mib / 1024} GB RAM · ${size.disk_gib} GB disk (${size.name})`;
export const workerSizeLabel = (size: Flavor) =>
  `${size.vcpus} vCPU · ${size.ram_mib / 1024} GB RAM (${size.name})`;
export const buildMachineLabel = (size: BuilderSize) =>
  `${size.flavor.vcpus} vCPU · ${size.flavor.ram_mib / 1024} GB · ${size.useDefault ? 'platform default' : 'set for this app'}`;

export function sizeGroups(sizes: Flavor[]) {
  const groups = new Map<string, Flavor[]>();
  for (const size of sizes) {
    const family = size.name.includes('.') ? size.name.split('.')[0] : 'Other';
    groups.set(family, [...(groups.get(family) ?? []), size]);
  }
  return [...groups]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([family, items]) => ({
      family,
      items: items.sort(
        (a, b) => a.vcpus - b.vcpus || a.ram_mib - b.ram_mib || a.name.localeCompare(b.name),
      ),
    }));
}

export const sizingApi = {
  sizes: (id: string) => request(`/apps/${id}/sizes`, (v) => flavors(record(v).items)),
  plan: (id: string, selected: string) =>
    request(`/apps/${id}/resize-plan?` + new URLSearchParams({ flavor: selected }), (v) => {
      const plan = fields(v, {
        applicationId: 'string',
        activation: 'string',
        allocation: 'string',
        fingerprint: 'string',
      });
      flavor(plan.flavor);
      fields(plan.current, {
        enabled: 'boolean',
        flavor: 'string',
        cpuMHz: 'number',
        memoryMiB: 'number',
      });
      fields(plan.reserve, {
        cpuMHzMinimum: 'number',
        memoryMiBMinimum: 'number',
        percentMinimum: 'number',
      });
      return plan as ResizePlan;
    }),
  builder: (id: string) =>
    request(`/apps/${id}/builder-size`, (v) => {
      const data = fields(v, { useDefault: 'boolean' });
      return {
        flavor: flavor(data.flavor),
        defaultFlavor: flavor(data.defaultFlavor),
        useDefault: data.useDefault as boolean,
      };
    }),
  setBuilder: (id: string, selected: string | null, expectedFlavor: string | null, key: string) =>
    request(`/apps/${id}/builder-size`, intentData, {
      method: 'PUT',
      key,
      body: { flavor: selected, expectedFlavor },
    }),
  defaultBuilder: () =>
    request('/settings/default-builder-size', (v) => {
      const data = record(v);
      return { flavor: flavor(data.flavor), sizes: flavors(data.sizes) };
    }),
  setDefaultBuilder: (selected: string, expectedFlavor: string, key: string) =>
    request('/settings/default-builder-size', intentData, {
      method: 'PUT',
      key,
      body: { flavor: selected, expectedFlavor },
    }),
};
