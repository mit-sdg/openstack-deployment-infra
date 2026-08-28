job "metadata-block-test" {
  region      = "global"
  datacenters = ["example-datacenter"]
  type        = "batch"

  constraint {
    attribute = "${meta.project_id}"
    operator  = "="
    value     = "22222222-2222-4222-8222-222222222222"
  }

  group "test" {
    task "metadata" {
      driver = "docker"
      config {
        image = "docker.io/library/alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce"
        command = "sh"
        args = ["-ec", "if wget -q -T 3 -O- http://169.254.169.254/latest/meta-data/; then echo metadata-was-reachable >&2; exit 1; else exit 0; fi"]
        readonly_rootfs = true
        security_opt = ["no-new-privileges:true"]
        cap_drop = ["all"]
      }
      resources {
        cpu    = 100
        memory = 64
      }
    }
  }
}
