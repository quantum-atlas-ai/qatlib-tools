# Cache contract

`qatlib-env prepare` verifies workspace identity, toolchain, dependency locks and local artifact manifests before restoring cache entries. `qatlib-env cache explain` reports the resolved cache identity and missing-module estimate; `qatlib-env cache upload` requires an explicit configured endpoint and credential.

Cache service URL, repository identity, bucket/prefix and credential variable are consumer configuration. The shared package never embeds endpoint or bucket values.
