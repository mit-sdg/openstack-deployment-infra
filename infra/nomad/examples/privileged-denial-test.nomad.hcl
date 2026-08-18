job "privileged-denial-test" {
  region      = "global"
  datacenters = ["csail-stata"]
  type        = "batch"

  constraint {
    attribute = "${meta.project_id}"
    operator  = "="
    value     = "22222222-2222-4222-8222-222222222222"
  }

  group "test" {
    restart {
      attempts = 0
      mode     = "fail"
    }
    task "privileged" {
      driver = "docker"
      config {
        image      = "docker.io/library/alpine@sha256:14358309a308569c32bdc37e2e0e9694be33a9d99e68afb0f5ff33cc1f695dce"
        command    = "true"
        privileged = true
      }
      resources {
        cpu    = 100
        memory = 64
      }
    }
  }
}
