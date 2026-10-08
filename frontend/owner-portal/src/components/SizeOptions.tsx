import { sizeGroups, sizeLabel, type Flavor } from '../sizingApi';

export function SizeOptions({
  sizes,
  current,
  disableCurrent = false,
}: {
  sizes: Flavor[];
  current?: Flavor;
  disableCurrent?: boolean;
}) {
  const choices =
    current && !sizes.some((size) => size.flavor_id === current.flavor_id)
      ? [...sizes, current]
      : sizes;
  return sizeGroups(choices).map(({ family, items }) => (
    <optgroup key={family} label={family}>
      {items.map((size) => (
        <option
          key={size.flavor_id}
          value={size.flavor_id}
          disabled={disableCurrent && size.flavor_id === current?.flavor_id}
        >
          {sizeLabel(size)}
          {size.flavor_id === current?.flavor_id ? ' (current)' : ''}
        </option>
      ))}
    </optgroup>
  ));
}
