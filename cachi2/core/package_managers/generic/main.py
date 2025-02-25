import asyncio
import logging
import os
from pathlib import Path
from typing import Union

import yaml
from packageurl import PackageURL
from pydantic import ValidationError

from cachi2.core.checksum import must_match_any_checksum
from cachi2.core.config import get_config
from cachi2.core.errors import PackageRejected
from cachi2.core.models.input import Request
from cachi2.core.models.output import RequestOutput
from cachi2.core.models.sbom import Component, ExternalReference
from cachi2.core.package_managers.general import async_download_files
from cachi2.core.package_managers.generic.models import GenericLockfileV1
from cachi2.core.rooted_path import RootedPath

log = logging.getLogger(__name__)
DEFAULT_LOCKFILE_NAME = "generic_lockfile.yaml"
DEFAULT_DEPS_DIR = "deps/generic"


def fetch_generic_source(request: Request) -> RequestOutput:
    """
    Resolve and fetch generic dependencies for a given request.

    :param request: the request to process
    """
    components = []
    for package in request.generic_packages:
        path = request.source_dir.join_within_root(package.path)
        components.extend(_resolve_generic_lockfile(path, request.output_dir))
    return RequestOutput.from_obj_list(components=components)


def _resolve_generic_lockfile(source_dir: RootedPath, output_dir: RootedPath) -> list[Component]:
    """
    Resolve the generic lockfile and pre-fetch the dependencies.

    :param source_dir: the source directory to resolve the lockfile from
    :param output_dir: the output directory to store the dependencies
    """
    lockfile_path = source_dir.join_within_root(DEFAULT_LOCKFILE_NAME)
    if not lockfile_path.path.exists():
        raise PackageRejected(
            f"Cachi2 generic lockfile '{DEFAULT_LOCKFILE_NAME}' missing, refusing to continue.",
            solution=(
                f"Make sure your repository has cachi2 generic lockfile '{DEFAULT_LOCKFILE_NAME}' checked in "
                "to the repository."
            ),
        )

    # output_dir is now the root and cannot be escaped
    output_dir = output_dir.re_root(DEFAULT_DEPS_DIR)

    log.info(f"Reading generic lockfile: {lockfile_path}")
    lockfile = _load_lockfile(lockfile_path, output_dir)
    to_download: dict[str, Union[str, os.PathLike[str]]] = {}

    for artifact in lockfile.artifacts:
        # create the parent directory for the artifact
        Path.mkdir(Path(artifact.target).parent, parents=True, exist_ok=True)
        to_download[str(artifact.download_url)] = artifact.target

    asyncio.run(async_download_files(to_download, get_config().concurrency_limit))

    # verify checksums
    for artifact in lockfile.artifacts:
        must_match_any_checksum(artifact.target, artifact.formatted_checksums)
    return _generate_sbom_components(lockfile)


def _load_lockfile(lockfile_path: RootedPath, output_dir: RootedPath) -> GenericLockfileV1:
    """
    Load the cachi2 generic lockfile from the given path.

    :param lockfile_path: the path to the lockfile
    :param output_dir: path to output directory
    """
    with open(lockfile_path, "r") as f:
        try:
            lockfile_data = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise PackageRejected(
                f"Cachi2 lockfile '{lockfile_path}' yaml format is not correct: {e}",
                solution="Check correct 'yaml' syntax in the lockfile.",
            )

        try:
            lockfile = GenericLockfileV1.model_validate(
                lockfile_data, context={"output_dir": output_dir}
            )
        except ValidationError as e:
            loc = e.errors()[0]["loc"]
            msg = e.errors()[0]["msg"]
            raise PackageRejected(
                f"Cachi2 lockfile '{lockfile_path}' format is not valid: '{loc}: {msg}'",
                solution=(
                    "Check the correct format and whether any keys are missing in the lockfile."
                ),
            )
    return lockfile


def _generate_sbom_components(lockfile: GenericLockfileV1) -> list[Component]:
    """Generate a list of SBOM components for a given lockfile."""
    components: list[Component] = []

    for artifact in lockfile.artifacts:
        name = Path(artifact.target).name
        url = str(artifact.download_url)
        checksums = ",".join([f"{algo}:{digest}" for algo, digest in artifact.checksums.items()])
        component = Component(
            name=name,
            purl=PackageURL(
                type="generic",
                name=name,
                qualifiers={
                    "download_url": url,
                    "checksums": checksums,
                },
            ).to_string(),
            type="file",
            external_references=[ExternalReference(url=url, type="distribution")],
        )
        components.append(component)

    return components
