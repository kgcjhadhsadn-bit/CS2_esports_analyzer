# Developer notes

| Path | What it is |
|---|---|
| `briefs/<package>.md` | The requirements each work package is built against (plus addenda when contracts changed). |
| `reports/<package>.md` | The implementing engineer's report: summary, **public API**, known limitations, open requests. Reviewers append a `## Review round N` section. |
| `STATUS.md` | Build status per package (implemented / reviewed / integrated). |

Package ownership and the interfaces between packages are in [../ARCHITECTURE.md](../ARCHITECTURE.md).
