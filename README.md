Addon dependencies distribution tool
------------------------------------

This tool is backend portion of Addon distribution of dependencies for [AYON launcher](https://github.com/ynput/ayon-launcher).

This collects info about all enabled addons on the [AYON server](https://github.com/ynput/ayon-docker) based on bundle name, reads their
pyproject.toml files, create one merged pyproject.toml (it tries to find common denominator for dependency version).

Then it uses Poetry to create new venv, zips it and provides this to AYON server for distribution.

It is expected to run on machine that has set reasonable development environment.

Required environment variables:
- AYON_SERVER_URL - AYON server url
- AYON_API_KEY - AYON api key for service account

For local development, use `.env` file. You can use `example_env` as base.

The tool should ideally run automatically and listen for events on the Server OR could be run manually on machine(s).

Entry point for manual triggering is `start.ps1` or `start.sh`.

Implemented commands:
- `install` - creates `./.venv` with requirements for this tool
- `create` - runs main process to create new dependency package and uploads it. Expects argument with name of Bundle (eg. `./start create -b MyBundle`). For more information `./start create --help`.
- `listen` - starts service connecting to Ayon server and listening for events to trigger main process (TBD)
- `list-bundles` - lists all bundles on Ayon server

TODO:
- [ ] force to reuse python version from Installer (make `pyenv` required)
- [ ] safe runtime dependencies
    - dependencies and runtime dependencies are not validated agains each other
    - single pip module can be installed in dependencies and in runtime dependencies
- [ ] skip dependency package creation if there are not any addons with dependencies
- [ ] Provide dockerized AYON service manageable by directly by [ASH (AYON service host)](https://github.com/ynput/ash)
