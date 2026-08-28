let
  contract = builtins.fromJSON (builtins.readFile ../../infra/lib/platform_contract.json);
  requiredSections = [
    "roles"
    "ports"
    "accounts"
    "executables"
    "directories"
    "installation"
    "protocol"
    "inventory"
  ];
  missingSections = builtins.filter (name: !(builtins.hasAttr name contract)) requiredSections;
in
assert contract.version == 1;
assert missingSections == [ ];
{
  roles = contract.roles.all;
  persistentRoles = contract.roles.persistent;
  inherit (contract)
    ports
    accounts
    executables
    directories
    installation
    protocol
    inventory
    ;
}
