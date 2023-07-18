import os
import click
import ayon_api
from ayon_api.constants import SERVER_URL_ENV_KEY, SERVER_API_ENV_KEY

from .core import create_package


@click.group()
def main_cli():
    pass


@main_cli.command(help="Create dependency package for release bundle")
@click.option(
    "-b",
    "--bundle-name",
    required=True,
    help="Bundle name for which dep package is created")
@click.option(
    "--server",
    help="AYON server url",
    envvar=SERVER_URL_ENV_KEY)
@click.option(
    "--api-key",
    help="Api key",
    envvar=SERVER_API_ENV_KEY)
def create(bundle_name, server, api_key):
    if server:
        os.environ[SERVER_URL_ENV_KEY] = server

    if api_key:
        os.environ[SERVER_API_ENV_KEY] = api_key

    if ayon_api.create_connection() is False:
        raise RuntimeError("Could not connect to server.")

    create_package(bundle_name)


def main():
    main_cli()
