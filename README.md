# OpenStack app platform for classes

This repository turns one OpenStack project into a small hosting platform for student web apps. Students sign in to a website, connect a GitHub repository, and deploy any commit. The platform builds the app, runs it on its own virtual machine with its own address, and gives it managed PostgreSQL, MongoDB, and S3 storage. Course staff operate the hosting infrastructure and control its data and credentials.

It was built for MIT's 6.1040 (Software Design) and runs that class's student apps. Nothing in it is specific to one class or domain.

![How the platform fits together](docs/images/architecture.svg)

## What it does

For **students** (app owners), the owner portal lets them:

- deploy an exact commit from a public or private GitHub repository, with Node.js or Bun at the version requested by their repository;
- see build output, logs, deployment history, and health, and roll back to an earlier commit;
- add a database (PostgreSQL or MongoDB) or S3 file storage, and set environment variables that are never shown again;
- share an app with teammates.

For **course staff**, the platform provides:

- a setup tool that builds and boot-tests every machine image from one Git commit, then creates the whole deployment in your OpenStack project;
- roles for students, staff, and portal admins, with app quotas, an audit log, and sign-in through Commons (the class site) or local accounts;
- separate encrypted backups for platform state, portal state, and app data, with off-site export and tested restore procedures;
- an operator command-line tool for health, host replacement, upgrades, and recovery, plus a read-only health dashboard.

## Start here

| If you want to… | Read |
| --- | --- |
| Understand what this is and how it works | [How it works](docs/how-it-works.md) |
| Decide whether you can run it for your class | [Plan a deployment](docs/guides/plan-a-deployment.md) |
| Set it up | [Deploy the platform](docs/guides/deploy-the-platform.md), then [Open the owner portal](docs/guides/open-the-portal.md) |
| Run it day to day | [Run the platform](docs/guides/run-the-platform.md) |
| Deploy an app as a student | [Deploy an app](docs/guides/for-app-owners.md) |
| Change the code | [Development](docs/development.md) |

The [documentation home](docs/README.md) lists every guide and reference page, and has a glossary.

## What you need

- An OpenStack project with room for three small persistent servers, one server per app, short-lived build servers, and three volumes (by default 32 GiB, 500 GiB, and 600 GiB).
- A domain, with HTTPS in front of the platform from a provider such as Cloudflare.
- An x86_64 Linux machine where an unprivileged account runs the setup and operator tools, with Nix, uv, and Python 3.14.

[Plan a deployment](docs/guides/plan-a-deployment.md) covers the details.

## Repository layout

| Directory | What's in it |
| --- | --- |
| `openstack_platform/` | The Python code: setup, the operator CLI, the application controller and its helper, and the owner portal services |
| `frontend/` | The owner portal and operator dashboard (React and TypeScript) |
| `nix/` | NixOS configurations for the five machine roles, and image builds |
| `infra/` | Scripts that setup and the machines run (OpenStack calls, backups, certificates, registry, monitoring) and the shared platform contract |
| `deploy/` | Scripts that install operator and helper releases |
| `config/` | Example configuration |
| `tests/` | The test suite |
| `docs/` | This documentation |

The [repository map](docs/reference/repository-map.md) describes every file.

## License

Licensed under the [Apache License 2.0](LICENSE).
