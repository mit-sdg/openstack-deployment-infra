{ platform, constants }:
let
  operator = constants.accounts.operator;
  controller = constants.accounts.controller;
  state = platform.paths.adminState;
  secrets = "${state}/operator/secrets";
  private = path: {
    kind = "normalize";
    inherit path;
    uid = operator.uid;
    allowedGids = [
      operator.gid
      controller.gid
    ];
    allowedModes = [
      384
      416
    ]; # 0600 / 0640
    gid = controller.gid;
    mode = 416;
  };
  directory =
    path:
    (private path)
    // {
      directory = true;
      allowedModes = [
        448
        488
      ]; # 0700 / 0750
      mode = 488;
    };
  public = path: {
    kind = "check";
    inherit path;
    uid = operator.uid;
    allowedModes = [ 420 ]; # 0644; group/link count do not govern read-only checks.
  };
  copy = name: {
    kind = "copy";
    source = "${state}/operator/${name}";
    destination = "${state}/controller/${name}";
    sourceUid = operator.uid;
    uid = controller.uid;
    gid = controller.gid;
  };
in
{
  version = 1;
  operations = [
    (copy "policy.json")
    (copy "image-selections.json")
    (directory secrets)
    (private "${secrets}/openstack.env")
    (private "${secrets}/nomad-tokens.env")
    (private "${secrets}/storage-bootstrap.env")
    (private "${secrets}/builder_operator_ed25519")
    (public "${secrets}/builder_operator_ed25519.pub")
    (directory "${secrets}/provisioning-pki")
    (private "${secrets}/provisioning-pki/nomad-cli-key.pem")
    (private "${secrets}/provisioning-pki/nomad-worker-key.pem")
    (public "${secrets}/provisioning-pki/internal-ca.pem")
    (public "${secrets}/provisioning-pki/nomad-cli.pem")
    (public "${secrets}/provisioning-pki/nomad-worker.pem")
  ];
}
