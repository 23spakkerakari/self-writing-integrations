# deploy/docker

One parameterized Dockerfile builds every carto service image (spec 6, 14.6):

```
docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=edge --build-arg ENTRY=carto-edge -t carto-edge:dev .
docker build -f deploy/docker/base.Dockerfile --build-arg MEMBER=core --build-arg ENTRY=carto-core -t carto-core:dev .
```

Images run as uid 10001 with a read-only root filesystem (writable volumes at `/var/lib/carto`
and, for the edge, the state directory), all capabilities dropped and `no-new-privileges` (see
`deploy/compose/compose.yaml`). The runtime stage keeps `/bin/sh` for the entrypoint shim; M6
replaces the base with a distroless image pinned by digest and adds cosign signatures and SLSA
provenance to `release.yml` (spec 14.8).
