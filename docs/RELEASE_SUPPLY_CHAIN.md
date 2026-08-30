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

The canonical manifest binds the full source commit, implementation contract, `uv.lock`, deterministic operator-wheel inputs, protocol-v1 helper actions, controller API and latest schema versions, an explicit not-shipped UI contract placeholder, and identities for all five role-image flake outputs. It also binds the generated Python dependency SBOM and provenance statement. The installer retains the manifest, signature, trust root, SBOM, and provenance below the completed release's `evidence/` directory.

Set these literal assignments in the private setup environment file:

```text
PLATFORM_RELEASE_MANIFEST=/private/releases/<commit>/release-manifest.json
PLATFORM_RELEASE_SIGNATURE=/private/releases/<commit>/release-manifest.sig
PLATFORM_RELEASE_TRUST_ROOT=/private/release-trust-root.pem
```

Pass the corresponding `--release-manifest`, `--release-signature`, and `--release-trust-root` options when invoking `install_release.py` directly. A changed manifest, source input, SBOM, provenance file, signature, trust root, commit, schema/API version, helper action, UI placeholder, or role identity is rejected before installation state changes.

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

Unsigned verification is refused when `PLATFORM_ENVIRONMENT=production`. Supplying signature or trust-root material with an unsigned manifest is also refused.

## Database migration and rollback order

Controller schema migrations are forward-only. Before selecting a release that raises `controller.schemaVersion`, stop mutation intake and take and verify a controller database backup. Then install the matching operator/helper release, migrate once, start the controller, and verify its API before replacing role images.

Rollback ordering is the reverse only while the database schema remains accepted by the old controller: replace role images first, then select the prior complete executable release. After a forward migration crosses the old controller's supported schema, do not start the old binary against the migrated database. Stop the controller, restore the pre-upgrade database by the offline restore procedure, and only then select/start the old executable release. Provider-state rollback and database restore remain separate operations.
