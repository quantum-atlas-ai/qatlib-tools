# `qatlib_forge`

Host-scoped GitHub and Gitea collaboration transport using the Python standard
library. `RepoRef` binds platform, host, owner, and repository; credentials are
resolved only for the selected host. `RepoAPI` covers pull requests, issues,
comments, branches, comparisons, archives, Actions runs/jobs, dispatches,
logs, and artifacts.

```python
from qatlib_forge.repo_api import RepoAPI, RepoRef

ref = RepoRef.parse("https://github.com/example/tools")
api = RepoAPI(ref)
issues = api.list_issues(state="open", labels=["needs-review"])
```

The command line exposes pull request, issue, repository, comparison, task
status, workflow run, bounded log, and artifact inspection commands. Downstream
packages can add their own command layer without making this shared transport
know repository-specific workflows.

