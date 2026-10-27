# pre-commit-hooks

Hooks for [prek](https://github.com/j178/prek) and [pre-commit](https://pre-commit.com).

## check-managed-files

Rejects changes to files that carry a marker in their header, such as files
generated from shared templates, unless the author is on an allowlist.

```yaml
repos:
  - repo: https://github.com/RegioHelden/pre-commit-hooks
    rev: v1.0.0
    hooks:
      - id: check-managed-files
        args:
          - --marker=@generated
          - --allow-author=sync-bot@example.com
          - --hint=edit the template it is generated from
```

| Option | Default | Meaning |
| --- | --- | --- |
| `--marker` | required | text that marks a file as managed |
| `--allow-author` | none | author emails that may change managed files; repeatable |
| `--hint` | none | appended to every rejection, e.g. how to change a managed file |
| `--header-lines` | 5 | lines searched for the marker |
| `--header-bytes` | 4096 | bytes read at most, for binary files without line breaks |
| `--from-ref`, `--to-ref` | | check the commits in this range instead of the staged changes |

Allowed authors can also come as a comma-separated list in
`MANAGED_FILES_HOOK_ALLOWED_AUTHORS`, so CI can set them without touching the
config.

Removing the marker from a file is how a project takes that file over: from then
on the hook ignores it.

### Where it runs

- **pre-commit** checks the staged changes against the author email git will record. While a
  merge is being concluded, only files that differ from both parents count, so
  managed updates that arrive with the merged branch pass.
- **pre-push** checks every commit being pushed against its author's email.
  Install it with `prek install --hook-type pre-commit --hook-type pre-push`.
- **CI** checks every commit of a merge request or pull request. Pass the range
  explicitly: a plain `prek run` has nothing staged in CI and skips the hook.

GitLab:

```yaml
check-managed-files:
  rules:
    - if: $CI_PIPELINE_SOURCE == "merge_request_event"
  script:
    - prek run check-managed-files
      --from-ref "$CI_MERGE_REQUEST_DIFF_BASE_SHA"
      --to-ref "${CI_MERGE_REQUEST_SOURCE_BRANCH_SHA:-$CI_COMMIT_SHA}"
```

GitHub:

```yaml
on: pull_request

jobs:
  check-managed-files:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v5
        with:
          fetch-depth: 0
      - run: pipx install prek
      - run: prek run check-managed-files --from-ref "$BASE" --to-ref "$HEAD"
        env:
          BASE: ${{ github.event.pull_request.base.sha }}
          HEAD: ${{ github.event.pull_request.head.sha }}
```

A job that runs every hook with `--all-files` cannot pass the range, as prek
refuses it alongside `--from-ref`. Set `MANAGED_FILES_HOOK_FROM_REF` and
`MANAGED_FILES_HOOK_TO_REF` on the job instead; only this hook reads them, and an
empty `MANAGED_FILES_HOOK_TO_REF` means the checked-out commit.

A shallow GitLab clone is deepened as needed. On GitHub, the pull request's base
may not be an ancestor of its head, so check out the full history.

### Limits

- Authors are whatever git records, and `git commit --author` sets any email. The
  hook stops mistakes, not someone determined to bypass it.
- Concluding a conflicted cherry-pick or revert of a managed update is rejected.
  Skip the hook for that commit with `SKIP=check-managed-files`.

## Development

The tests need only Python 3.9 or newer and git. From the repository root:

```sh
python -m unittest
```

To run them on the oldest supported Python:

```sh
uv run --no-project --python 3.9 python -m unittest
```

## License

[MIT](LICENSE)
