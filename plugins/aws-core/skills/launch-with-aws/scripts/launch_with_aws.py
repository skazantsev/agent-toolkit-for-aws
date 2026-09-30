#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Launch with AWS — CLI entry point.

Subcommands mirror the migration workflow steps. Each prints JSON to stdout
on success, exits non-zero with error message on stderr on failure.

Dependencies:
    pip install boto3
"""

import json
import logging
import os
import sys
from pathlib import Path
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# The scripts use PEP 604 union syntax (`str | None`) in signatures, which
# fails at definition time on Python < 3.10; boto3 also requires 3.10+.
if sys.version_info < (3, 10):
    print(
        f"Error: Python 3.10+ required "
        f"(found {sys.version_info.major}.{sys.version_info.minor})",
        file=sys.stderr,
    )
    sys.exit(2)

try:
    import boto3  # noqa: F401
except ImportError:
    print(
        "Error: missing required package: boto3\n" "Install it with:\n" "  pip install boto3",
        file=sys.stderr,
    )
    sys.exit(2)

# Ensure sibling modules are importable regardless of cwd.
sys.path.insert(0, str(Path(__file__).parent))

import launch_api_client as api
from archive import ArchiveError, parse_github_url, zip_local_repo
from auth import SessionExpiredError, session_status, sign_out, start_auth, wait_for_auth
from launch_config import (
    SUPPORTED_REGIONS,
    ConfigError,
    ResolvedRegion,
    load_config,
    resolve_region,
    save_region,
)

# Global flags, stripped from argv before a command sees its positional args.
_FLAGS: dict[str, str] = {}


def _ok(value) -> None:
    """Print a JSON result and exit 0."""
    print(json.dumps(value, indent=2, default=str))
    sys.exit(0)


def _fail(message: str) -> None:
    """Print error to stderr and exit 1."""
    print(json.dumps({"error": message}), file=sys.stderr)
    sys.exit(1)


def _resolved_region() -> ResolvedRegion:
    """Resolve the region for this invocation from the global flags."""
    return resolve_region(explicit=_FLAGS.get("region"), aws_mcp_url=_FLAGS.get("aws-mcp-url"))


def _region() -> str:
    return _resolved_region().region


def _default_repo_name(source: str) -> str:
    github = parse_github_url(source)
    if github:
        return github[1]
    cleaned = source.rstrip("/").rstrip(os.sep)
    return os.path.basename(cleaned) or "uploaded-app"


# ── Subcommands ──────────────────────────────────────────────────────────


def cmd_resolve_region() -> None:
    """Report the region a new launch would run in, for the upload confirmation."""
    resolved = _resolved_region()
    _ok(
        {
            "region": resolved.region,
            "regionName": resolved.region_name,
            "source": resolved.source,
            "sourceLabel": resolved.source_label,
            "baseUrl": load_config(resolved.region).base_url,
        }
    )


def cmd_auth_start() -> None:
    """Start authentication (non-blocking)."""
    config = load_config(_region())
    result = start_auth()
    result["baseUrl"] = config.base_url
    _ok(result)


def cmd_auth_wait(pid: str) -> None:
    """Wait for interactive authentication to complete."""
    config = load_config(_region())
    result = wait_for_auth(pid=int(pid))
    result["baseUrl"] = config.base_url
    _ok(result)


def cmd_session_status() -> None:
    """Report the local session state without triggering authentication."""
    _ok(session_status())


def cmd_sign_out() -> None:
    """Sign out and delete the local session; requires re-auth next use."""
    _ok(sign_out())


def cmd_create_launch(source: str, name: str | None = None) -> None:
    """Create a launch from a local path or GitHub URL.

    For local paths, zips and uploads first. For GitHub URLs, passes directly.
    """
    display_name = (name or "").strip() or _default_repo_name(source)
    resolved = _resolved_region()
    region = resolved.region

    github = parse_github_url(source)
    if github:
        # GitHub URL — pass as gitHub source directly.
        launch_source = {"gitHub": {"repositoryUrl": source}}
    elif urlparse(source).scheme in ("http", "https"):
        _fail(
            f"Unsupported repository URL: {source}. Provide a "
            "https://github.com/owner/name URL or a local directory path."
        )
        return
    else:
        # Local directory — zip, upload, then pass as s3Upload source.
        archive = zip_local_repo(source, display_name)
        target = api.create_upload_url(region=region)
        api.put_archive(target["uploadUrl"], archive)
        launch_source = {"s3Upload": {"uploadId": target["uploadId"]}}

    result = api.create_launch(name=display_name, source=launch_source, region=region)
    launch = result.get("launch", result)
    # The customer confirmed this region before the upload, so make it the default
    # for later runs, and tell the agent which region the launch now lives in.
    save_region(region)
    launch["region"] = region
    launch["regionName"] = resolved.region_name
    _ok(launch)


def cmd_get_launch(launch_id: str, include: str | None = None) -> None:
    """Get launch details, optionally including specific sections."""
    result = api.get_launch(launch_id, include=include, region=_region())
    _ok(result.get("launch", result))


def cmd_list_launches() -> None:
    """List launches for the current user, across both regions unless one is given."""
    regions = [_FLAGS["region"]] if _FLAGS.get("region") else list(SUPPORTED_REGIONS)

    items = []
    next_tokens = {}
    errors = {}
    for region in regions:
        try:
            result = api.list_launches(region=region)
        except api.ApiError as err:
            # One unreachable region must not hide the launches in the other.
            errors[region] = str(err)
            continue
        for launch in result.get("items", []):
            launch["region"] = region
            items.append(launch)
        if result.get("nextToken"):
            next_tokens[region] = result["nextToken"]

    output: dict = {"items": items}
    if next_tokens:
        output["nextTokens"] = next_tokens
    if errors:
        output["errors"] = errors
    _ok(output)


def cmd_delete_launch(launch_id: str) -> None:
    """Delete a launch."""
    region = _region()
    api.delete_launch(launch_id, region=region)
    _ok({"deleted": True, "id": launch_id, "region": region})


def cmd_refine_plan(launch_id: str, *context_pairs: str) -> None:
    """Refine a launch plan with context answers (key=value pairs)."""
    context_answers = {}
    for pair in context_pairs:
        if "=" in pair:
            key, value = pair.split("=", 1)
            context_answers[key.strip()] = value.strip()
    result = api.refine_plan(launch_id, context_answers=context_answers or None, region=_region())
    _ok(result.get("launch", {}))


def cmd_start_launch_execution(launch_id: str) -> None:
    """Start execution of a launch's deployment plan."""
    _ok(api.start_launch_execution(launch_id, region=_region()).get("launch", {}))


def cmd_get_launch_status(launch_id: str) -> None:
    """Poll launch status including execution progress."""
    raw = api.get_launch(launch_id, include="execution,cost_estimate", region=_region())
    result = raw.get("launch", raw)
    status = result.get("status")
    execution = result.get("execution")

    output = {
        "id": result.get("id"),
        "status": status,
        "isComplete": status == "completed",
        "isFailed": status == "failed",
    }

    if execution:
        output["completedTasks"] = execution.get("completedTasks")
        output["totalTasks"] = execution.get("totalTasks")
        output["currentPhase"] = execution.get("currentPhase")

    if result.get("costEstimate"):
        output["costEstimate"] = result["costEstimate"]

    if result.get("failureReason"):
        output["failureReason"] = result["failureReason"]

    if result.get("contextInputs"):
        output["contextInputs"] = result["contextInputs"]

    _ok(output)


def cmd_get_launch_download_url(launch_id: str) -> None:
    """Get the download URL for a completed launch."""
    raw = api.get_launch(launch_id, include="download_url", region=_region())
    result = raw.get("launch", raw)
    download_url = result.get("downloadUrl")
    if not download_url:
        _fail("Download URL not available yet. Ensure the launch execution has completed.")
        return
    _ok({"downloadUrl": download_url})


# ── CLI dispatcher ───────────────────────────────────────────────────────

# (func, min_required_args)
from typing import Any, Callable

COMMANDS: dict[str, tuple[Callable[..., Any], int]] = {
    "resolve-region": (cmd_resolve_region, 0),
    "auth-start": (cmd_auth_start, 0),
    "auth-wait": (cmd_auth_wait, 1),
    "session-status": (cmd_session_status, 0),
    "sign-out": (cmd_sign_out, 0),
    "create-launch": (cmd_create_launch, 1),
    "get-launch": (cmd_get_launch, 1),
    "list-launches": (cmd_list_launches, 0),
    "delete-launch": (cmd_delete_launch, 1),
    "refine-plan": (cmd_refine_plan, 1),
    "start-launch-execution": (cmd_start_launch_execution, 1),
    "get-launch-status": (cmd_get_launch_status, 1),
    "get-launch-download-url": (cmd_get_launch_download_url, 1),
}


# Flags that may appear anywhere in the arguments, not just before the command.
_GLOBAL_FLAGS = ("--region", "--aws-mcp-url")


def _parse_global_flags(args: list[str]) -> tuple[list[str], dict[str, str]]:
    """Split the argument list into positional args and global flag values."""
    positional: list[str] = []
    flags: dict[str, str] = {}
    index = 0
    while index < len(args):
        arg = args[index]
        if arg in _GLOBAL_FLAGS:
            if index + 1 >= len(args):
                _fail(f"Missing value for {arg}")
            flags[arg.lstrip("-")] = args[index + 1]
            index += 2
            continue
        positional.append(arg)
        index += 1
    return positional, flags


def _usage() -> str:
    return (
        "Usage: launch_with_aws.py <command> [args...] "
        f'[{" | ".join(f"{flag} <region>" for flag in _GLOBAL_FLAGS)}]\n\n'
        "Commands:\n" + "\n".join(f"  {name}" for name in COMMANDS)
    )


def main() -> None:
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(_usage())
        sys.exit(0)

    args, flags = _parse_global_flags(args)
    _FLAGS.update(flags)
    if not args:
        _fail("Missing command")

    command = args[0]
    if command not in COMMANDS:
        print(f"Unknown command: {command}\n\n{_usage()}", file=sys.stderr)
        sys.exit(1)

    func, min_args = COMMANDS[command]
    cmd_args = args[1:]

    if len(cmd_args) < min_args:
        _fail(f"Missing required argument for {command}")

    try:
        func(*cmd_args)
    except SessionExpiredError as err:
        _fail(str(err))
    except ArchiveError as err:
        _fail(str(err))
    except ConfigError as err:
        _fail(str(err))
    except api.ApiError as err:
        hint = ""
        if err.status == 401:
            hint = (
                " Hint: the backend rejected the Bearer token. Ensure you "
                "signed in successfully."
            )
        _fail(f"{err}{hint}")
    except Exception:
        # Log the full exception locally; return a generic message.
        logger.debug("Unhandled error in command %s", command, exc_info=True)
        _fail("The operation failed. Re-run with logging enabled for details.")


if __name__ == "__main__":
    main()
