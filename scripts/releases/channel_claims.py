"""A channel claim's one channel request: allocated at admission, then built and published.

A channel build is a Stable Release run on ``channel.<stamp>-vX.Y.Z`` (release.py --channel NAME
--build-commit REV). Admission allocates its request in the channel CAS (creating the channel with
the claim's branding if it is new); the request's release tag is the claim, so its packages carry
the claim's version and stamped Windows quad and live in the claim's tag archive. A write-once
pointer in that archive, ``releases/tag/<claim>/channel-request.json``, makes the allocation
idempotent across reruns, and every later job binds itself to it (require_admitted_request).
"""
from __future__ import annotations

import hashlib

from hermes_cli.release_channels import ChannelReader, canonical_json, decode_json, require_sha256


def pointer_key(tag: str) -> str:
    return f"releases/tag/{tag}/channel-request.json"


def _publisher(env: dict):
    from scripts.releases import r2
    from scripts.releases.channels import ChannelPublisher, R2ChannelStore
    from scripts.releases.r2_scope import channel_public_base

    def authorize(action: str, record: dict) -> None:
        # Admission only ever creates a preview channel and allocates in one; promotion is
        # publish-channel's, after the build.
        if action not in {"create", "allocate"} or record["policy"] != "preview":
            raise ValueError("Channel admission only creates and allocates preview channels")

    return ChannelPublisher(R2ChannelStore(*r2.credentials()), env["GITHUB_REPOSITORY"],
                            channel_public_base(), authorize=authorize)


def admit_request(admitted: dict, env: dict) -> dict[str, str]:
    """Allocate (or find again) the admitted channel claim's request; the admit job's outputs."""
    publisher = _publisher(env)
    publisher.create(admitted["channel"], admitted["branding"])
    request = publisher.allocate_claim(admitted["channel"], admitted["claim_tag"], admitted["commit"],
                                       admitted["version"].split("+", 1)[0], admitted["bundle_env"],
                                       env.get("GITHUB_SHA"))
    return {"channel-build": request["buildId"],
            "channel-request-sha256": hashlib.sha256(canonical_json(request)).hexdigest()}


def read_admitted_request(tag: str, public_base: str, repository: str) -> tuple[dict, str]:
    """The request the claim's pointer names, read back from the public origin and validated."""
    reader = ChannelReader(public_base, repository=repository)
    pointer = decode_json(reader.read_bytes(pointer_key(tag)))
    if not isinstance(pointer, dict) or set(pointer) != {"schema", "buildId", "sha256"} or pointer["schema"] != 1:
        raise ValueError("Invalid channel claim request pointer")
    from scripts.releases.channel_publish import read_request

    digest = require_sha256(pointer["sha256"])
    return read_request(pointer["buildId"], digest, public_base, repository), digest


def require_admitted_request(tag: str, claim: dict, env: dict) -> dict:
    """Refuse a leg that would build anything but exactly this claim's admitted request."""
    from scripts.releases.r2_scope import channel_public_base

    request, digest = read_admitted_request(tag, channel_public_base(), env["GITHUB_REPOSITORY"])
    if (env.get("CHANNEL_BUILD"), env.get("CHANNEL_REQUEST_SHA256")) != (request["buildId"], digest):
        raise ValueError("Channel request differs from the one admitted for this claim")
    expected = {"releaseTag": tag, "channel": claim["channel"], "commit": claim["commit"],
                "bundleEnv": claim.get("bundle_env") or {}}
    if any(request.get(key) != value for key, value in expected.items()):
        raise ValueError("Channel request does not match its claim")
    return request
