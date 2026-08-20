# Releasing picoharness to PyPI

The name is claimed by the **first successful upload**, not by registering an
account. Until then it is free for anyone.

## Before the first upload

The metadata is filled in: the URLs point at `lars-kiaer/picoharness`, and the
author and the licence say Lars Kiær. Two things remain.

1. Create the GitHub repository at `github.com/lars-kiaer/picoharness`, so the
   four URLs in `[project.urls]` resolve. PyPI does not check them, but a
   visitor will.
2. Create a PyPI account and turn on 2FA. It is mandatory.

## Build and check

```bash
pip install build twine
rm -rf dist/
python -m build              # makes dist/*.whl and dist/*.tar.gz
twine check dist/*           # must say PASSED for both
```

Confirm the wheel holds the whole package, not only `__init__.py`:

```bash
python -c "import zipfile,glob; print(*sorted(zipfile.ZipFile(glob.glob('dist/*.whl')[0]).namelist()), sep='\n')"
```

Then install the wheel in a clean environment and import it:

```bash
python -m venv /tmp/t && /tmp/t/bin/pip install dist/*.whl
/tmp/t/bin/python -c "from picoharness.memory import EpisodicIndex; print('ok')"
/tmp/t/bin/pico-failures list
```

## Upload

```bash
twine upload dist/*          # user: __token__   password: your API token
```

Make the token **project-scoped** after the first upload. The first one has to
be account-scoped, because the project does not exist yet.

## After the first upload

Set up **Trusted Publishing** on PyPI: it lets GitHub Actions publish through
OIDC, so no API token is stored anywhere. PyPI project settings -> Publishing.

## Rules that bite

- **A version can never be re-uploaded.** Not even after you delete it. A
  mistake in 0.1.0 means 0.1.1, so check the build before you push.
- **TestPyPI is a separate registry.** An upload there does **not** reserve the
  name on PyPI. Use it to rehearse, then upload for real.
- **`.gitignore` decides what goes in the wheel.** Hatchling respects it. An
  unanchored `memory/` line matches `src/picoharness/memory/` and drops the
  whole subpackage, and the build still succeeds. Anchor runtime paths with a
  leading slash: `/memory/`. This bug is already fixed here; do not undo it.
