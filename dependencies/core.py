import os
import re
import tempfile
import copy
import platform
import hashlib
import zipfile
import json
import subprocess
import collections
import shutil
from typing import Dict, Union
from packaging import version
from dataclasses import dataclass

import toml
from poetry.core.constraints.version import parse_constraint

import ayon_api
from ayon_api import create_dependency_package_basename

from .utils import (
    run_subprocess,
    ZipFileLongPaths,
    get_venv_executable,
    get_venv_site_packages,
)


@dataclass
class Bundle:
    name: str
    addons: Dict[str, str]
    dependency_packages: Dict[str, str]
    installer_version: Union[str, None]


def get_bundles(con):
    """Provides dictionary with available bundles

    Returns:
        (dict) of (Bundle) {"BUNDLE_NAME": Bundle}
    """
    bundles_by_name = {}
    for bundle_dict in con.get_bundles()["bundles"]:
        try:
            bundle = Bundle(
                name=bundle_dict["name"],
                installer_version=bundle_dict["installerVersion"],
                addons=bundle_dict["addons"],
                dependency_packages=bundle_dict["dependencyPackages"],
            )
        except KeyError:
            print(f"Wrong bundle definition for {bundle_dict['name']}")
            continue
        bundles_by_name[bundle.name] = bundle
    return bundles_by_name


def get_all_addon_tomls(con):
    """Provides list of dict containing addon tomls.

    Returns:
        (dict) of (dict)
    """

    tomls = {}
    response = con.get_addons_info(details=True)
    for addon_dict in response["addons"]:
        addon_name = addon_dict["name"]
        addon_versions = addon_dict["versions"]

        for version_name, addon_version_dict in addon_versions.items():
            client_pyproject = addon_version_dict.get("clientPyproject")
            if not client_pyproject:
                continue
            full_name = f"{addon_name}_{version_name}"
            tomls[full_name] = client_pyproject

    return tomls


def get_bundle_addons_tomls(con, bundle):
    """Query addons for `bundle` to get their python dependencies.

    Returns:
        dict[str, dict[str, Any]]: {'core_1.0.0': {...toml content...}}
    """

    bundle_addons = {
        f"{key}_{value}"
        for key, value in bundle.addons.items()
        if value is not None
    }
    addon_tomls = get_all_addon_tomls(con)

    return {
        addon_full_name: toml
        for addon_full_name, toml in addon_tomls.items()
        if addon_full_name in bundle_addons
    }


def find_installer_by_name(con, bundle_name, installer_name):
    for installer in con.get_installers()["installers"]:
        if installer["version"] == installer_name:
            return installer
    raise ValueError(f"{bundle_name} must have installer present.")


def get_installer_toml(installer):
    """Returns dict with format matching of .toml file for `installer_name`.

    Queries info from server for `bundle_name` and its `installer_name`,
    transforms its list of python dependencies into dictionary matching format
    of `.toml`

    Example output:
        {"tool": {"poetry": {"dependencies": {"somepymodule": "1.0.0"...}}}}

    Args:
        installer (dict[str, Any])

    Returns:
        dict[str, Any]: Installer toml content.
    """

    return {
        "tool": {
            "poetry": {
                # Create copy to avoid modifying original data
                "dependencies": copy.deepcopy(installer["pythonModules"])
            }
        },
        "ayon": {
            "runtimeDependencies": copy.deepcopy(
                installer["runtimePythonModules"]
            )
        }
    }


def is_valid_toml(toml):
    """Validates that 'toml' contains all required fields.

    Args:
        toml (dict[str, Any])

    Returns:
        True if all required keys present

    Raises:
        KeyError
    """

    required_fields = ["tool.poetry"]
    for field in required_fields:
        fields = field.split(".")
        value = toml
        while fields:
            key = fields.pop(0)
            value = value.get(key)

            if not value:
                raise KeyError(f"Toml content must contain {field}")

    return True


def merge_tomls(main_toml, addon_toml, addon_name):
    """Add dependencies from 'addon_toml' to 'main_toml'.

    Looks for mininimal compatible version from both tomls.

    Handles sections:
        - ["tool"]["poetry"]["dependencies"]
        - ["tool"]["poetry"]["dev-dependencies"]
        - ["ayon"]["runtimeDependencies"]

    Returns:
        (dict): updated 'main_toml' with additional/updated dependencies

    Raises:
        ValueError if any tuple of main and addon dependency cannot be resolved
    """

    dependency_keyes = ["dependencies", "dev-dependencies"]
    for key in dependency_keyes:
        main_poetry = main_toml["tool"]["poetry"].get(key) or {}
        addon_poetry = addon_toml["tool"]["poetry"].get(key) or {}
        for dependency, dep_version in addon_poetry.items():
            if main_poetry.get(dependency):
                main_version = main_poetry[dependency]
                resolved_vers = _get_correct_version(main_version, dep_version)
            else:
                main_version = "N/A"
                resolved_vers = dep_version

            resolved_vers = str(resolved_vers)
            if dependency == "python":
                resolved_vers = "3.9.*"  # TEMP TODO

            if resolved_vers == "<empty>":
                raise ValueError(
                    f"Version {dep_version} cannot be resolved against"
                    f" {main_version} for {dependency} in {addon_name}"
                )

            main_poetry[dependency] = resolved_vers

        main_toml["tool"]["poetry"][key] = main_poetry

    # handle runtime dependencies
    platform_name = platform.system().lower()

    addon_poetry = addon_toml.get("ayon", {}).get("runtimeDependencies")
    if not addon_poetry:
        return main_toml

    main_poetry = main_toml["ayon"]["runtimeDependencies"]
    for dependency, dep_info in addon_poetry.items():
        if main_poetry.get(dependency):
            if dep_info.get(platform_name):
                dep_version = dep_info[platform_name]["version"]
                main_version = (
                    main_poetry[dependency][platform_name]["version"])
            else:
                dep_version = dep_info["version"]
                main_version = main_poetry[dependency]["version"]

            result_range = _get_correct_version(main_version, dep_version)
            if (
                str(result_range) != "<empty>"
                and parse_constraint(dep_version).allows(result_range)
            ):
                dep_info = main_poetry[dependency]
            else:
                raise ValueError(
                    f"Cannot result {dependency} with"
                    f" {dep_info} for {addon_name}"
                )

        if dep_info:
            main_poetry[dependency] = dep_info

    return main_toml


def _get_correct_version(main_version, dep_version):
    """Return resolved version from two version (constraint).

    Arg:
        main_version (str): version or constraint ("3.6.1", "^3.7")
        dep_version (str): dtto

    Returns:
        (VersionRange| EmptyConstraint if cannot be resolved)
    """

    if isinstance(dep_version, dict):
        dep_version = dep_version["version"]
    if isinstance(main_version, dict):
        main_version = main_version["version"]
    if dep_version and _is_url_constraint(dep_version):
        # custom location for addon should take precedence
        return dep_version

    if main_version and _is_url_constraint(main_version):
        return main_version

    if not main_version:
        return parse_constraint(dep_version)
    if not dep_version:
        return parse_constraint(main_version)
    return parse_constraint(dep_version).intersect(
        parse_constraint(main_version)
    )


def _is_url_constraint(version):
    version = str(version)
    return "http" in version or "git" in version


def _version_parse(version_value):
    """Handles different formats of versions

    Parses:
        "^2.0.0"
        { version = "301", markers = "sys_platform == 'win32'" }
    """
    if isinstance(version_value, dict):
        return version_value.get("version")
    return version.parse(version_value)


def get_full_toml(base_toml_data, addon_tomls):
    """Loops through list of local addon folder paths to create full .toml

    Full toml is used to calculate set of python dependencies for all enabled
    addons.

    Args:
        base_toml_data (dict): content of pyproject.toml in the root
        addon_tomls (dict): content of addon pyproject.toml

    Returns:
        (dict) updated base .toml
    """

    for addon_name, addon_toml_data in addon_tomls.items():
        if isinstance(addon_toml_data, str):
            addon_toml_data = toml.loads(addon_toml_data)
        base_toml_data = merge_tomls(
            base_toml_data, addon_toml_data, addon_name)

    return base_toml_data


def prepare_new_venv(full_toml_data, venv_folder):
    """Let Poetry create new venv in 'venv_folder' from 'full_toml_data'.

    Args:
        full_toml_data (dict): toml representation calculated based on basic
            .toml + all addon tomls
        venv_folder (str): path where venv should be created

    Raises:
        RuntimeError: Exception is raised if process finished with nonzero
            return code.
    """

    toml_path = os.path.join(venv_folder, "pyproject.toml")
    tool_poetry = {
        "name": "AYONDepPackage",
        "version": "1.0.0",
        "description": "Dependency package for AYON",
        "authors": ["Ynput s.r.o. <info@openpype.io>"],
        "license": "MIT License",
    }
    full_toml_data["tool"]["poetry"].update(tool_poetry)

    _convert_url_constraints(full_toml_data)

    with open(toml_path, "w") as fp:
        fp.write(toml.dumps(full_toml_data))

    poetry_bin = os.path.join(os.getenv("POETRY_HOME"), "bin", "poetry")
    venv_path = os.path.join(venv_folder, ".venv")
    env = dict(os.environ.items())
    run_subprocess(
        [poetry_bin, "run", "python", "-m", "venv", venv_path],
        env=env
    )
    env["VIRTUAL_ENV"] = venv_path
    for cmd in (
        [poetry_bin, "config", "virtualenvs.create", "false", "--local"],
        [poetry_bin, "config", "virtualenvs.in-project", "false", "--local"],
    ):
        run_subprocess(cmd, env=env)

    run_subprocess(
        [poetry_bin, "config", "--list"],
        env=env,
        cwd=venv_path
    )
    return run_subprocess(
        [poetry_bin, "install", "--no-root", "--ansi"],
        env=env,
        cwd=venv_path
    )

def _convert_url_constraints(full_toml_data):
    """Converts string occurences of "git+https" to dict required by Poetry"""
    dependency_keyes = ["dependencies", "dev-dependencies"]
    for key in dependency_keyes:
        dependencies = full_toml_data["tool"]["poetry"].get(key) or {}
        for dependency, dep_version in dependencies.items():
            dep_version = str(dep_version)
            if not _is_url_constraint(dep_version):
                continue

            try:
                # TODO there is probably better way how to handle this
                # Dictionary from requirements.txt contains raw string
                # - "{'git': 'https://...'}"
                dep_version = json.loads(dep_version.replace("'", '"'))
            except ValueError:
                pass

            if isinstance(dep_version, dict):
                dependencies[dependency] = dep_version
                continue

            revision = None
            if "@" in dep_version:
                parts = dep_version.split("@")
                dep_version = parts.pop(0)
                revision = "@".join(parts)

            if dep_version.startswith("http"):
                dependencies[dependency] = {"url": dep_version}
                continue

            if "git+" in dep_version:
                dep_version = dep_version.replace("git+", "")
                dependencies[dependency] = {"git": dep_version}
                continue

            if revision:
                dependencies[dependency]["rev"] = revision


def lock_to_toml_data(lock_path):
    """Create toml file with explicit version from lock file.

    Should be used to compare addon venv with client venv and purge existing
    libraries.

    Args:
        lock_path (str): path to base lock file (from build)
    Returns:
        (dict): dictionary representation of toml data with explicit library
            versions
    Raises:
        (FileNotFound)
    """

    if not os.path.exists(lock_path):
        raise ValueError(
            f"{lock_path} doesn't exist. Provide path to real toml."
        )

    with open(lock_path) as fp:
        parsed = toml.load(fp)

    dependencies = {
        package_info["name"]: package_info["version"]
        for package_info in parsed["package"]
    }

    return {"tool": {"poetry": {"dependencies": dependencies}}}


def remove_existing_from_venv(addons_venv_path, installer):
    """Loop through calculated addon venv and remove already installed libs.

    Args:
        addons_venv_path (str): path to newly created merged venv for active
            addons
        installer (dict[str, Any]): installer data from server.

    Returns:
        (set) of folder/file paths that were removed from addon venv, used only
            for testing
    """

    pip_executable = get_venv_executable(addons_venv_path, "pip")
    print("Removing packages from venv")
    print("\n".join([
        f"- {package_name}"
        for package_name in sorted(installer["pythonModules"])
    ]))
    for package_name in installer["pythonModules"]:
        run_subprocess(
            [pip_executable, "uninstall", package_name, "--yes"],
            bound_output=False
        )


def zip_venv(venv_folder, zip_filepath):
    """Zips newly created venv to single .zip file."""

    site_packages_roots = get_venv_site_packages(venv_folder)
    with ZipFileLongPaths(zip_filepath, "w", zipfile.ZIP_DEFLATED) as zipf:
        for site_packages_root in site_packages_roots:
            sp_root_len_start = len(site_packages_root) + 1
            for root, _, filenames in os.walk(site_packages_root):
                # Care only about files
                if not filenames:
                    continue

                # Skip __pycache__ folders
                root_name = os.path.basename(root)
                if root_name == "__pycache__":
                    continue

                dst_root = ""
                if len(root) > sp_root_len_start:
                    dst_root = root[sp_root_len_start:]

                for filename in filenames:
                    src_path = os.path.join(root, filename)
                    dst_path = os.path.join("dependencies", dst_root, filename)
                    zipf.write(src_path, dst_path)


def prepare_zip_venv(tmpdir):
    """Handles creation of zipped venv.

    Args:
        tmpdir (str): temp folder path

    Returns:
        (str) path to zipped venv
    """

    zip_file_name = f"{create_dependency_package_basename()}.zip"
    venv_zip_path = os.path.join(tmpdir, zip_file_name)
    print(f"Zipping new venv to {venv_zip_path}")
    zip_venv(os.path.join(tmpdir, ".venv"), venv_zip_path)

    return venv_zip_path


def create_addons_venv(full_toml_data, tmpdir):
    print(f"Preparing new venv in {tmpdir}")
    return_code = prepare_new_venv(full_toml_data, tmpdir)
    if return_code != 0:
        raise RuntimeError(f"Preparation of {tmpdir} failed!")
    return os.path.join(tmpdir, ".venv")


def get_applicable_package(con, new_toml):
    """Compares existing dependency packages to find matching.

    One dep package could contain same versions of python dependencies for
    different versions of addons (eg. no change in dependency, but change in
    functionality)

    Args:
        new_toml (dict): in a format of regular toml file
    Returns:
        (str) name of matching package
    """
    toml_python_packages = dict(
        sorted(new_toml["tool"]["poetry"]["dependencies"].items())
    )
    for package in con.get_dependency_packages()["packages"]:
        package_python_packages = dict(sorted(
            package["pythonModules"].items())
        )
        if toml_python_packages == package_python_packages:
            return package


def get_python_modules(venv_path):
    """Uses pip freeze to get installed libraries from `venv_path`.

    Args:
        venv_path (str): absolute path to created dependency package already
            with removed libraries from installer package
    Returns:
        (dict) {'acre': '1.0.0',...}
    """

    pip_executable = get_venv_executable(venv_path, "pip")

    process = subprocess.Popen(
        [pip_executable, "freeze", venv_path, "--no-color"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    _stdout, _stderr = process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"Failed to freeze pip packages.")

    packages = {}
    for line in _stdout.decode("utf-8").split("\n"):
        line = line.strip()
        if not line:
            continue

        match = re.match(r"^(.+?)(?:==|>=|<=|~=|!=|@)(.+)$", line)
        if match:
            package_name, version = match.groups()
            packages[package_name.rstrip()] = version.lstrip()
        else:
            packages[line] = None

    return packages


def calculate_hash(file_url):
    checksum = hashlib.md5()
    with open(file_url, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            checksum.update(chunk)
    return checksum.hexdigest()


def prepare_package_data(venv_zip_path, bundle):
    """Creates package data for server.

    All data in output are used to call 'create_dependency_package'.

    Args:
        venv_zip_path (str): Local path to zipped venv.
        bundle (Bundle): Bundle object with all data.

    Returns:
          dict[str, Any]: Dependency package information.
    """

    venv_path = os.path.join(os.path.dirname(venv_zip_path), ".venv")
    python_modules = get_python_modules(venv_path)

    platform_name = platform.system().lower()
    package_name = os.path.basename(venv_zip_path)
    checksum = calculate_hash(venv_zip_path)

    return {
        "filename": package_name,
        "python_modules": python_modules,
        "source_addons": bundle.addons,
        "installer_version": bundle.installer_version,
        "checksum": checksum,
        "checksum_algorithm": "md5",
        "file_size": os.stat(venv_zip_path).st_size,
        "platform_name": platform_name,
    }


def stored_package_to_dir(
    output_dir, venv_zip_path, bundle, package_data
):
    """Store dependency package to output directory.

    A json file with dependency package information is created and stored
    next to the dependency package file (replaced extension with .json).

    Bundle name is added to dependency package before saving.

    Args:
        output_dir (str): Path where dependency package will be stored.
        venv_zip_path (str): Local path to zipped venv.
        bundle (Bundle): Bundle object with all data.
        package_data (dict[str, Any]): Dependency package information.
    """

    if not os.path.exists(output_dir):
        os.makedirs(output_dir)

    new_package_data = copy.deepcopy(package_data)
    # Change data to match server requirements
    new_package_data["platform"] = new_package_data.pop("platform_name")
    new_package_data["size"] = new_package_data.pop("file_size")
    # Add bundle name as information
    new_package_data["bundle_name"] = bundle.name

    filename = new_package_data["filename"]
    output_path = os.path.join(output_dir, filename)
    shutil.copy(venv_zip_path, output_path)
    metadata_path = output_path + ".json"
    with open(metadata_path, "w") as stream:
        json.dump(new_package_data, stream, indent=4)


def upload_to_server(con, venv_zip_path, package_data):
    """Creates and uploads package on the server

    Args:
        con (ayon_api.ServerAPI): Connection to server.
        venv_zip_path (str): Local path to zipped venv.
        package_data (dict[str, Any]): Package information.

    Returns:
        str: Package name.
    """

    con.create_dependency_package(**package_data)
    con.upload_dependency_package(
        venv_zip_path,
        package_data["filename"],
        package_data["platform_name"]
    )


def update_bundle_with_package(con, bundle, package_data):
    """Assign `package_name` to `bundle`

    Args:
        con (ayon_api.ServerAPI)
        bundle (Bundle)
        package_data (dict[str, Any])
    """

    package_name = package_data["filename"]
    print(f"Updating in {bundle.name} with {package_name}")
    platform_name = package_data["platform_name"]
    dependency_packages = copy.deepcopy(bundle.dependency_packages)
    dependency_packages[platform_name] = package_name
    con.update_bundle(bundle.name, dependency_packages)


def is_file_deletable(filepath):
    """Can be file deleted.

    Args:
        filepath (str): Path to a file.

    Returns:
        bool: File can be removed.
    """

    file_dirname = os.path.dirname(filepath)
    if os.access(file_dirname, os.W_OK | os.X_OK):
        try:
            with open(filepath, "w"):
                pass
            return True
        except OSError:
            pass

    return False


def _remove_tmpdir(tmpdir):
    """Safer removement of temp directory.

    Notes:
        @iLLiCiTiT Function was created because I've hit issues with
            'shutil.rmtree' on tmpdir -> lead to many un-cleared temp dirs.

    Args:
        tmpdir (str): Path to temp directory.
    """

    failed = []
    if not os.path.exists(tmpdir):
        return failed

    filepaths = set()
    for root, dirnames, filenames in os.walk(tmpdir):
        for filename in filenames:
            filepaths.add(os.path.join(root, filename))

    remove_queue = collections.deque()
    for filepath in filepaths:
        remove_queue.append((filepath, 0))

    while remove_queue:
        (filepath, attempt) = remove_queue.popleft()
        try:
            os.remove(filepath)
        except OSError:
            if attempt > 3:
                failed.append(filepath)
            else:
                remove_queue.append((filepath, attempt + 1))

    if not failed:
        shutil.rmtree(tmpdir)
    return failed


def create_package(bundle_name, con=None, output_dir=None, skip_upload=False):
    """
        Pulls all active addons info from server, provides their pyproject.toml
    (if available), takes base (installer) pyproject.toml, adds tomls from
    addons.
    Builds new venv with dependencies only for addons (dependencies already
    present in build are filtered out).
    Uploads zipped venv back to server.

    Args:
        bundle_name (str): Name of bundle for which is package created.
        con (Optional[ayon_api.ServerAPI]): Prepared server API object.
        output_dir (Optional[str]): Path to directory where package will be
            created.
        skip_upload (Optional[bool]): Skip upload to server. Default: False.
    """

    if con is None:
        con = ayon_api.get_server_api_connection()
    bundles_by_name = get_bundles(con)

    bundle = bundles_by_name.get(bundle_name)
    if not bundle:
        raise ValueError(f"{bundle_name} not present on the server.")

    bundle_addons_toml = get_bundle_addons_tomls(con, bundle)

    # Installer is not set, dependency package cannot be created
    if bundle.installer_version is None:
        print(f"Bundle '{bundle.name}' does not have set installer.")
        return None

    installer = find_installer_by_name(
        con, bundle_name, bundle.installer_version)
    installer_toml_data = get_installer_toml(installer)
    full_toml_data = get_full_toml(installer_toml_data, bundle_addons_toml)

    applicable_package = get_applicable_package(con, full_toml_data)
    if applicable_package:
        update_bundle_with_package(con, bundle, applicable_package)
        return applicable_package["filename"]

    # create resolved venv based on distributed venv with Desktop + activated
    # addons
    tmpdir = tempfile.mkdtemp(prefix="ayon_dep-package")

    print(">>> Creating processing directory {}".format(tmpdir))

    addons_venv_path = create_addons_venv(full_toml_data, tmpdir)

    # remove already distributed libraries from addons specific venv
    remove_existing_from_venv(addons_venv_path, installer)

    venv_zip_path = prepare_zip_venv(tmpdir)

    package_data = prepare_package_data(venv_zip_path, bundle)
    if output_dir:
        stored_package_to_dir(output_dir, venv_zip_path, bundle, package_data)

    if not skip_upload:
        upload_to_server(con, venv_zip_path, package_data)
        update_bundle_with_package(con, bundle, package_data)

    print(">>> Cleaning up processing directory {}".format(tmpdir))
    failed_paths = _remove_tmpdir(tmpdir)
    if failed_paths:
        print("Failed to cleanup tempdir: {}".format(tmpdir))
        print("\n".join(sorted(failed_paths)))

    return package_data["filename"]
