# Fork-Safe CI/CD for NSB

This repo now includes GitHub Actions intended for a personal or lab-maintained
fork of NSB. Every workflow is guarded with:

```yaml
if: github.repository_owner != 'nsb-ucsc'
```

That means the jobs will not run inside the upstream `nsb-ucsc/nsb` repository,
even if these workflow files are copied or opened in a pull request there.

## Remote Layout

Keep the original repository as `upstream` and make it fetch-only. Point
`origin` at your own fork.

```bash
git remote add origin https://github.com/<your-github-user>/nsb.git
git remote set-url --push origin https://github.com/<your-github-user>/nsb.git
git remote -v
```

Expected result:

```text
origin   https://github.com/<your-github-user>/nsb.git (fetch)
origin   https://github.com/<your-github-user>/nsb.git (push)
upstream https://github.com/nsb-ucsc/nsb.git (fetch)
upstream DISABLED (push)
```

## Workflows

### `Fork CI`

File: `.github/workflows/fork-ci.yml`

- Runs on `push`, `pull_request`, and manual dispatch.
- Builds NSB on `ubuntu-latest` and `macos-14`.
- Installs the generated Python client package and verifies
  `import nsb_client` and `import proto.nsb_pb2`.

### `Fork Packages`

File: `.github/workflows/fork-packages.yml`

- Runs manually or when you push a tag matching `v*`.
- Produces:
  - Python wheel + sdist for `nsb-client`
  - Linux C++ install tarball
  - `SHA256SUMS.txt`
- Uploads all package files as workflow artifacts.
- On tag pushes, also publishes the files to a GitHub release in your fork.

Suggested tag format:

```text
v0.1.0-fork.1
```

### `Fork Performance`

File: `.github/workflows/fork-performance.yml`

- Runs manually from the Actions tab.
- Checks out this repo plus a companion perf harness repo.
- Clones `ns-3-dev`, injects the scratch target needed for
  `examples/ns3/nsb-testing-tcp.cc`, builds everything, and runs the sweep.
- Uploads CSV, plots, and per-run logs as artifacts.
- Can fail the workflow when optional RTT, CPU, memory, or drop-rate thresholds
  are exceeded.

## Companion Perf Repo

The performance workflow expects a companion repo containing the harness from
`nsb-ns3-perf-testing`. By default it looks for:

```text
<your-github-user>/nsb-ns3-perf-testing
```

If your fork uses a different repo name, provide `perf_repo` when manually
running the workflow.

## First-Time Fork Setup

1. Create your GitHub fork of `nsb-ucsc/nsb`.
2. Create or fork the companion `nsb-ns3-perf-testing` repo into your account.
3. Add your fork as `origin` locally.
4. Push a branch to your fork and confirm `Fork CI` runs there.
5. Push a `v*` tag when you want package artifacts and a release in your fork.
6. Use `Fork Performance` when you want a full ns-3 sweep and result artifacts.

## Notes

- The performance workflow uses `config-ns3-perf.yaml`, which keeps the daemon
  in the no-database configuration used for CI-style perf runs.
- The Python package build depends on generated protobuf stubs, so the package
  workflow builds the repo before creating wheel/sdist artifacts.
