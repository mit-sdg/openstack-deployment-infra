# Generate and verify release supply-chain evidence

Production setup and release installation accept one component set only after verifying `release-manifest.json` with an operator-selected Ed25519 public key. Verification occurs before setup creates local state or calls Nix/OpenStack, and before the release installer creates or changes release paths.

## Generate production evidence

From the clean release commit, keep the private signing key outside the repository:

```sh
commit=$(git rev-parse HEAD)
python3 -m openstack_platform.release_manifest generate \
  --repository "$PWD" --commit "$commit" \
  --output /private/releases/"$commit" \
  --signing-key /private/release-signing-key.pem
openssl pkey -in /private/release-signing-key.pem -pubout \
  -out /private/release-trust-root.pem
python3 -m openstack_platform.release_manifest verify \
  --repository "$PWD" --commit "$commit" \
  --manifest /private/releases/"$commit"/release-manifest.json \
  --signature /private/releases/"$commit"/release-manifest.sig \
  --trust-root /private/release-trust-root.pem
```

The pre-build component manifest binds the full source commit, implementation contract, `uv.lock`, deterministic operator-wheel inputs, protocol-v1 helper actions, controller API and latest schema versions, and an explicit not-shipped UI contract placeholder. Its role entries identify build inputs only; they are not artifact identities. The component SBOM is deterministic SPDX 2.3 Python dependency evidence.

After building all five role outputs, record for each role the QCOW2 path, `nix path-info --json --recursive` output, output store path, and exact canonical Glance publication metadata in a JSON object keyed by `admin`, `ingress`, `storage`, `worker`, and `builder`. Generate the post-build manifest:

```sh
python3 -m openstack_platform.release_manifest artifact-generate \
  --component-manifest /private/releases/"$commit"/release-manifest.json \
  --inputs /private/releases/"$commit"/artifact-inputs.json \
  --output /private/releases/"$commit"/artifacts \
  --signing-key /private/release-signing-key.pem
python3 -m openstack_platform.release_manifest artifact-verify \
  --component-manifest /private/releases/"$commit"/release-manifest.json \
  --manifest /private/releases/"$commit"/artifacts/role-artifacts.json \
  --signature /private/releases/"$commit"/artifacts/role-artifacts.sig \
  --trust-root /private/release-trust-root.pem
```

The post-build manifest binds each actual QCOW2 SHA-256, normalized Nix output and recursive closure identity, source component-manifest digest, and publication metadata. Its SPDX 2.3 SBOM combines Python packages with every unique Nix closure path/NAR identity; its in-toto/SLSA-style provenance names all QCOW2 and closure digests. Setup recomputes these identities after each build, and the publisher verifies the signed role record before upload. Glance acceptance requires its SHA-256 content hash and artifact metadata to match before setup selects the image.

Package the exact signed evidence for CI or another host; do not put manifests or
SBOMs in CI secrets:

```sh
python3 -m openstack_platform.release_manifest bundle-create \
  --source /private/releases/"$commit" \
  --output /private/releases/"$commit"/release-evidence.tar
sha256sum /private/releases/"$commit"/release-evidence.tar
```

Publish that immutable tar at an HTTPS URL. Configure protected environment
variables `RELEASE_EVIDENCE_URL`, `RELEASE_EVIDENCE_SHA256`, and
`RELEASE_TRUST_ROOT_PEM`; the trust root is public verification material, not a
signing secret. No manifest, SBOM, provenance, or private signing key belongs in
CI secrets. The protected publication environment therefore supplies only the
URL, digest, and public Ed25519 trust root. CI
refuses redirects, downloads at most 128 MiB, extracts only the eight exact
bounded regular evidence files into an absent private directory, and verifies
both signatures and the source commit before building or publishing an image.

Set these literal assignments in the private setup environment file:

```text
PLATFORM_RELEASE_MANIFEST=/private/releases/<commit>/release-manifest.json
PLATFORM_RELEASE_SIGNATURE=/private/releases/<commit>/release-manifest.sig
PLATFORM_RELEASE_TRUST_ROOT=/private/release-trust-root.pem
PLATFORM_ARTIFACT_MANIFEST=/private/releases/<commit>/artifacts/role-artifacts.json
PLATFORM_ARTIFACT_SIGNATURE=/private/releases/<commit>/artifacts/role-artifacts.sig
PLATFORM_ARTIFACT_TRUST_ROOT=/private/release-trust-root.pem
```

Pass the corresponding `--release-manifest`, `--release-signature`, and `--release-trust-root` options when invoking `install_release.py` directly. A changed component/artifact manifest, source input, SBOM, provenance file, signature, trust root, commit, schema/API version, helper action, UI placeholder, QCOW2, Nix closure, or publication projection is rejected before the affected setup/publication acceptance step.

## Unsigned development evidence

Unsigned mode is not a production fallback. Generate a manifest carrying both `releaseChannel: development-unsigned` and `warning: NOT FOR PRODUCTION`:

```sh
python3 -m openstack_platform.release_manifest generate \
  --repository "$PWD" --commit "$(git rev-parse HEAD)" \
  --output /tmp/platform-development-evidence \
  --unsigned-development
```

Verification additionally requires `--allow-unsigned-development`, or this exact setup assignment:

```text
PLATFORM_ALLOW_UNSIGNED_DEVELOPMENT=I_UNDERSTAND_THIS_IS_NOT_PRODUCTION
```

Use the same `--unsigned-development` option with `artifact-generate` for post-build development evidence. Unsigned component and artifact verification both require the exact acknowledgement. Verification is refused when `PLATFORM_ENVIRONMENT=production`; supplying signature or trust-root material with an unsigned manifest is also refused.

## Database migration and rollback order

Controller schema migrations are forward-only. Before selecting a release that raises `controller.schemaVersion`, stop mutation intake and take and verify a controller database backup. Then install the matching operator/helper release, migrate once, start the controller, and verify its API before replacing role images.

Rollback ordering is the reverse only while the database schema remains accepted by the old controller: replace role images first, then select the prior complete executable release. After a forward migration crosses the old controller's supported schema, do not start the old binary against the migrated database. Stop the controller, restore the pre-upgrade database by the offline restore procedure, and only then select/start the old executable release. Provider-state rollback and database restore remain separate operations.
