"""Canonical parser and validator for Hermes acceptance contracts.

The Kanban delivery gate, the standalone contract linter, and the acceptance
runner all import this module.  Keeping one parser matters: a contract that is
accepted by the runner must mean exactly the same thing to the terminal
delivery gate.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

import yaml
from yaml.constructor import ConstructorError
from yaml.tokens import (
    AliasToken,
    AnchorToken,
    BlockEndToken,
    BlockMappingStartToken,
    BlockSequenceStartToken,
    FlowMappingEndToken,
    FlowMappingStartToken,
    FlowSequenceEndToken,
    FlowSequenceStartToken,
)


VALID_DOMAINS = {"audit", "coding", "research", "ops", "docs"}
VALID_TARGETS = {
    "github-merge",
    "github-merge-and-deploy",
    "artifact-file",
    "end-state-probe",
    "rendered-doc",
}
REQUIRED_TOP_KEYS = {"domain", "target", "tier1", "tier2", "tier3"}
OPTIONAL_TOP_KEYS = {
    "deployment",
    "deployment-environment",
    "deployment-verifier",
}
TOP_KEYS = REQUIRED_TOP_KEYS | OPTIONAL_TOP_KEYS
TIER1_KEYS = {"cmd", "expect_exit"}
VALID_DEPLOYMENT_VALUES = {"required", "production"}
MAX_CONTRACT_BYTES = 64 * 1024
MAX_TIER1_ITEMS = 64
MAX_TIER2_ITEMS = 128
MAX_COMMAND_BYTES = 8 * 1024
MAX_YAML_NESTING = 32
MAX_YAML_TOKENS = 4096
DOMAIN_TARGETS = {
    # Coding can never opt out of review/merge by spelling its output as an
    # artifact. Read-only source inspections use the explicit audit domain.
    "coding": {"github-merge", "github-merge-and-deploy"},
    "audit": {"artifact-file", "end-state-probe", "rendered-doc"},
    "research": {"artifact-file", "end-state-probe", "rendered-doc"},
    "ops": {
        "artifact-file", "end-state-probe", "rendered-doc",
        "github-merge", "github-merge-and-deploy",
    },
    "docs": {
        "artifact-file", "rendered-doc", "github-merge",
        "github-merge-and-deploy",
    },
}

FENCE_RE = re.compile(
    r"```acceptance-contract[ \t]*\r?\n(.*?)\r?\n?```",
    re.DOTALL,
)


class _UniqueKeySafeLoader(yaml.SafeLoader):
    """Safe YAML loader that rejects duplicate and non-string mapping keys."""


def _construct_unique_mapping(
    loader: _UniqueKeySafeLoader,
    node: yaml.MappingNode,
    deep: bool = False,
) -> dict[str, Any]:
    mapping: dict[str, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str):
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                "mapping keys must be strings",
                key_node.start_mark,
            )
        if key in mapping:
            raise ConstructorError(
                "while constructing a mapping",
                node.start_mark,
                f"duplicate mapping key: {key}",
                key_node.start_mark,
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeySafeLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def extract_contract_text(body: str | None) -> tuple[str | None, list[str], int]:
    """Return YAML text, errors, and the number of contract fences found."""
    if not body:
        return None, ["card body is empty"], 0
    blocks = FENCE_RE.findall(body)
    if not blocks:
        return None, ["no ```acceptance-contract fenced block found"], 0
    if len(blocks) > 1:
        return (
            None,
            [f"{len(blocks)} acceptance-contract blocks found; exactly 1 required"],
            len(blocks),
        )
    if len(blocks[0].encode("utf-8")) > MAX_CONTRACT_BYTES:
        return (
            None,
            [f"acceptance-contract exceeds {MAX_CONTRACT_BYTES} bytes"],
            1,
        )
    return blocks[0], [], 1


def canonical_hash(obj: Any) -> str:
    canon = json.dumps(
        obj,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _validate(contract: Any) -> list[str]:
    errors: list[str] = []
    if not isinstance(contract, dict):
        return [f"contract YAML must be a mapping, got {type(contract).__name__}"]

    unknown = sorted(set(contract) - TOP_KEYS)
    if unknown:
        errors.append(f"unknown top-level keys: {unknown}")
    for key in sorted(REQUIRED_TOP_KEYS - set(contract)):
        errors.append(f"missing required key: {key}")

    domain = contract.get("domain")
    if "domain" in contract and domain not in VALID_DOMAINS:
        errors.append(
            f"domain must be one of {sorted(VALID_DOMAINS)}, got {domain!r}"
        )
    target = contract.get("target")
    if "target" in contract and target not in VALID_TARGETS:
        errors.append(
            f"target must be one of {sorted(VALID_TARGETS)}, got {target!r}"
        )
    if domain in DOMAIN_TARGETS and target in VALID_TARGETS:
        if target not in DOMAIN_TARGETS[domain]:
            errors.append(
                f"target {target!r} is not valid for domain {domain!r}; "
                f"expected one of {sorted(DOMAIN_TARGETS[domain])}"
            )

    tier1 = contract.get("tier1")
    if "tier1" in contract:
        if not isinstance(tier1, list) or not tier1:
            errors.append("tier1 must be a non-empty list of {cmd, expect_exit} mappings")
        elif len(tier1) > MAX_TIER1_ITEMS:
            errors.append(f"tier1 may contain at most {MAX_TIER1_ITEMS} commands")
        else:
            for index, item in enumerate(tier1):
                if not isinstance(item, dict):
                    errors.append(f"tier1[{index}] must be a mapping")
                    continue
                extra = sorted(set(item) - TIER1_KEYS)
                if extra:
                    errors.append(f"tier1[{index}] unknown keys: {extra}")
                cmd = item.get("cmd")
                if not isinstance(cmd, str) or not cmd.strip():
                    errors.append(f"tier1[{index}].cmd must be a non-empty string")
                elif len(cmd.encode("utf-8")) > MAX_COMMAND_BYTES:
                    errors.append(
                        f"tier1[{index}].cmd exceeds {MAX_COMMAND_BYTES} bytes"
                    )
                expect_exit = item.get("expect_exit")
                if not isinstance(expect_exit, int) or isinstance(expect_exit, bool):
                    errors.append(f"tier1[{index}].expect_exit must be an integer")

    tier2 = contract.get("tier2")
    if "tier2" in contract:
        if not isinstance(tier2, list):
            errors.append("tier2 must be a list of strings")
        elif len(tier2) > MAX_TIER2_ITEMS:
            errors.append(f"tier2 may contain at most {MAX_TIER2_ITEMS} items")
        else:
            for index, item in enumerate(tier2):
                if not isinstance(item, str) or not item.strip():
                    errors.append(f"tier2[{index}] must be a non-empty string")

    tier3 = contract.get("tier3")
    if "tier3" in contract and (
        not isinstance(tier3, str) or not tier3.strip()
    ):
        errors.append("tier3 must be a non-empty string")

    deployment = contract.get("deployment")
    deployment_keys = OPTIONAL_TOP_KEYS & set(contract)
    if "deployment" in contract and deployment not in VALID_DEPLOYMENT_VALUES:
        errors.append(
            "deployment must be one of "
            f"{sorted(VALID_DEPLOYMENT_VALUES)}, got {deployment!r}"
        )
    deploy_target = target == "github-merge-and-deploy"
    if deployment_keys and not deploy_target:
        errors.append(
            "deployment fields are only valid when target is "
            "'github-merge-and-deploy'"
        )
    if deploy_target:
        if deployment not in VALID_DEPLOYMENT_VALUES:
            errors.append(
                "deployment is required when target is "
                "'github-merge-and-deploy'"
            )
        environment = contract.get("deployment-environment")
        if deployment == "production" and environment is None:
            environment = "production"
        if not isinstance(environment, str) or not environment.strip():
            errors.append(
                "deployment-environment is required for a deployment contract"
            )
        verifier = contract.get("deployment-verifier")
        if not isinstance(verifier, str) or not verifier.strip():
            errors.append("deployment-verifier is required for a deployment contract")

    return errors


def lint_body(body: str | None) -> dict[str, Any]:
    """Parse and validate exactly one fenced YAML acceptance contract."""
    text, errors, block_count = extract_contract_text(body)
    if errors:
        return {
            "present": block_count > 0,
            "valid": False,
            "contract_hash": None,
            "errors": errors,
            "contract": None,
        }
    try:
        tokens = list(yaml.scan(text, Loader=_UniqueKeySafeLoader))
        if len(tokens) > MAX_YAML_TOKENS:
            return {
                "present": True,
                "valid": False,
                "contract_hash": None,
                "errors": [f"YAML contains more than {MAX_YAML_TOKENS} tokens"],
                "contract": None,
            }
        if any(
            isinstance(token, (AnchorToken, AliasToken))
            for token in tokens
        ):
            return {
                "present": True,
                "valid": False,
                "contract_hash": None,
                "errors": ["YAML anchors and aliases are not allowed"],
                "contract": None,
            }
        depth = 0
        for token in tokens:
            if isinstance(
                token,
                (
                    BlockMappingStartToken,
                    BlockSequenceStartToken,
                    FlowMappingStartToken,
                    FlowSequenceStartToken,
                ),
            ):
                depth += 1
                if depth > MAX_YAML_NESTING:
                    return {
                        "present": True,
                        "valid": False,
                        "contract_hash": None,
                        "errors": [
                            f"YAML nesting exceeds {MAX_YAML_NESTING} levels"
                        ],
                        "contract": None,
                    }
            elif isinstance(
                token,
                (BlockEndToken, FlowMappingEndToken, FlowSequenceEndToken),
            ):
                depth = max(0, depth - 1)
        contract = yaml.load(text, Loader=_UniqueKeySafeLoader)
    except (yaml.YAMLError, RecursionError) as exc:
        return {
            "present": True,
            "valid": False,
            "contract_hash": None,
            "errors": [f"YAML parse error: {exc}"],
            "contract": None,
        }
    errors = _validate(contract)
    is_mapping = isinstance(contract, dict)
    return {
        "present": True,
        "valid": not errors,
        # Invalid graphs (including recursive YAML aliases) are never hashed:
        # JSON canonicalization of a cycle raises instead of returning a
        # fail-closed lint result.
        "contract_hash": (
            canonical_hash(contract) if is_mapping and not errors else None
        ),
        "errors": errors,
        "contract": contract if is_mapping else None,
    }
