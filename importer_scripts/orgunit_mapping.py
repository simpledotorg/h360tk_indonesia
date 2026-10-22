"""Optional leaf -> central org unit mapping applied before an import runs.

The central admin maintains ``config/orgunit_mapping/mapping.yaml``, keyed by the
leaf node's ``source_key``.  Each block lists rules that pin one org unit coming
from that leaf node's ``orgunit.csv`` onto an explicit central ``org_units``
record, placed under a named hierarchy.

``targetOrgUnitHierarchy`` lists ancestor names top first.  It may spell out
the full chain from the top level, or start part-way down at an org unit that
another rule in the same block places — ``["District-1"]`` then means "under
District-1, wherever the District-1 rule puts it" — see _complete_hierarchies.

If the file is missing or empty there is nothing to map and the importer keeps
its existing behaviour.
"""

import logging
import os
from dataclasses import dataclass, replace

import yaml

log = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAPPING_PATH = os.getenv(
    'ORGUNIT_MAPPING_PATH',
    os.path.join(_REPO_ROOT, 'config', 'orgunit_mapping', 'mapping.yaml'),
).strip()

ID_SELECTOR = 'sourceOrgUnitId'
NAME_SELECTOR = 'sourceOrgUnitName'
TARGET_ID = 'targetOrgUnitId'
TARGET_NAME = 'targetOrgUnitName'
TARGET_HIERARCHY = 'targetOrgUnitHierarchy'


class OrgUnitMappingError(ValueError):
    """Raised when mapping.yaml exists but cannot be used as written."""


@dataclass(frozen=True)
class MappingRule:
    """One rule from a source key's ``mappings`` list.

    Exactly one of the two source selectors is set; the other is None.
    The target is the central record with target_org_unit_id when that is set,
    created if it does not exist yet.  Without an id the target is found — or
    created — by target_org_unit_name at its position, or by the leaf org
    unit's own name when the rule only lists a hierarchy.

    Only the target fields a rule lists are applied: target_org_unit_name names
    a record being created (an existing record keeps its name) and
    target_org_unit_hierarchy sets its position (an empty tuple is the top
    level).  A field left out is None and changes nothing.
    Rules returned by load_mapping_config always hold the full ancestor chain
    from the top level, even where mapping.yaml lists only the nearest ones.
    """

    source_org_unit_id: int | None
    source_org_unit_name: str | None
    target_org_unit_id: int | None
    target_org_unit_name: str | None
    target_org_unit_hierarchy: tuple[str, ...] | None

    @property
    def target_level(self) -> int | None:
        """Level the hierarchy places the record at, or None when none is listed."""
        if self.target_org_unit_hierarchy is None:
            return None
        return len(self.target_org_unit_hierarchy) + 1

    def describe(self) -> str:
        if self.source_org_unit_id is not None:
            selector = f'{ID_SELECTOR}={self.source_org_unit_id}'
        else:
            selector = f'{NAME_SELECTOR}={self.source_org_unit_name!r}'
        if self.target_org_unit_id is None and self.target_org_unit_name is None:
            target = 'own name'
        elif self.target_org_unit_id is None:
            target = f'name {self.target_org_unit_name!r}'
        elif self.target_org_unit_name is None:
            target = f'id {self.target_org_unit_id}'
        else:
            target = f'id {self.target_org_unit_id} named {self.target_org_unit_name!r}'
        if self.target_org_unit_hierarchy is None:
            return f'[{selector} -> {target}]'
        under = ' > '.join(self.target_org_unit_hierarchy) or '(root)'
        return f'[{selector} -> {target} under {under}]'


def load_mapping_config(path: str | None = None) -> dict[str, list[MappingRule]]:
    """Read mapping.yaml into ``{source_key: [MappingRule, ...]}``.

    Returns an empty dict when the file is missing or empty, which turns the
    mapping step off; raises OrgUnitMappingError when it exists but is unusable.
    """
    path = path or MAPPING_PATH

    if not os.path.isfile(path):
        log.info('No org unit mapping file at %s — org units import unmapped.', path)
        return {}

    with open(path, 'r', encoding='utf-8') as mapping_file:
        try:
            raw = yaml.safe_load(mapping_file)
        except yaml.YAMLError as e:
            raise OrgUnitMappingError(f'{path} is not valid YAML: {e}') from e

    if not raw:
        log.info('Org unit mapping file %s is empty — org units import unmapped.', path)
        return {}

    if not isinstance(raw, dict):
        raise OrgUnitMappingError(
            f'{path}: top level must be a mapping of source_key -> rules'
        )

    config = {
        str(source_key): _parse_rules(path, str(source_key), block)
        for source_key, block in raw.items()
    }

    log.info(
        'Loaded org unit mapping from %s — %s',
        path,
        ', '.join(f'{key}: {len(rules)} rule(s)' for key, rules in config.items()),
    )
    return config


def find_matching_rule(
    rules: list[MappingRule],
    leaf_org_unit_id: int,
    leaf_org_unit_name: str,
) -> MappingRule | None:
    """Match one org unit against its source key's rules.

    Every ID selector is tried before any name selector, so an ID rule wins over
    a name rule that would also match.  None means the org unit is not mapped and
    imports against the leaf node's own hierarchy.
    """
    by_id = (rule for rule in rules if rule.source_org_unit_id == leaf_org_unit_id)
    by_name = (rule for rule in rules if rule.source_org_unit_name == leaf_org_unit_name)
    return next(by_id, None) or next(by_name, None)


def _parse_rules(path: str, source_key: str, block) -> list[MappingRule]:
    if block is None:
        return []
    if not isinstance(block, dict):
        raise OrgUnitMappingError(
            f"{path}: '{source_key}' must be a mapping containing a 'mappings' list"
        )

    raw_rules = block.get('mappings') or []
    if not isinstance(raw_rules, list):
        raise OrgUnitMappingError(f"{path}: '{source_key}.mappings' must be a list")

    where = f'{path}: {source_key}.mappings'
    rules = [
        _parse_rule(raw_rule, f'{where}[{position}]')
        for position, raw_rule in enumerate(raw_rules, start=1)
    ]
    return _complete_hierarchies(rules, where)


def _complete_hierarchies(rules: list[MappingRule], where: str) -> list[MappingRule]:
    """Give every rule its full ancestor chain from the top level.

    When the first name in a rule's targetOrgUnitHierarchy is the
    targetOrgUnitName of another rule in the same block that lists a hierarchy,
    that rule's ancestors are put in front, repeatedly up to the top level.

    A list is kept exactly as written — the meaning it had before partial lists
    were allowed — when:
      * no other rule targets its first name, or the only ones that do list no
        hierarchy (their position is not known until import);
      * it already starts with that rule's full path, e.g. a full chain, or a
        first name whose rule is at the top level;
      * the only rules with that name sit under this rule's own position, as
        when a region, district and facility share one name.

    Raises OrgUnitMappingError rather than guess when the first name matches
    rules at different positions, or when rules place each other in a loop.
    """
    completed: dict[int, tuple[str, ...] | None] = {}
    in_progress: list[int] = []

    def label(index: int) -> str:
        return f'mappings[{index + 1}] ({rules[index].target_org_unit_name!r})'

    def complete(index: int) -> tuple[str, ...] | None:
        if index not in completed:
            in_progress.append(index)
            try:
                completed[index] = completed_hierarchy(index)
            finally:
                in_progress.pop()
        return completed[index]

    def completed_hierarchy(index: int) -> tuple[str, ...] | None:
        rule = rules[index]
        hierarchy = rule.target_org_unit_hierarchy
        if not hierarchy:
            return hierarchy

        top = hierarchy[0]
        candidates = [
            other
            for other, candidate in enumerate(rules)
            if other != index
            and candidate.target_org_unit_name == top
            and candidate.target_org_unit_hierarchy is not None
            and not _same_target(candidate, rule)
        ]
        if any(
            _starts_with(hierarchy, rules[other].target_org_unit_hierarchy + (top,))
            for other in candidates
        ):
            return hierarchy

        own_path = (
            hierarchy + (rule.target_org_unit_name,)
            if rule.target_org_unit_name is not None
            else None
        )
        placements: dict[tuple[str, ...], list[int]] = {}
        for other in candidates:
            if other in in_progress:
                loop = in_progress[in_progress.index(other):] + [other]
                raise OrgUnitMappingError(
                    f'{where}: targetOrgUnitHierarchy entries place these rules '
                    f'under each other in a loop: {" -> ".join(map(label, loop))} '
                    '— list the full chain from the top level in one of them'
                )
            ancestors = complete(other)
            if own_path is not None and _starts_with(ancestors, own_path):
                continue
            if _starts_with(hierarchy, ancestors + (top,)):
                return hierarchy
            placements.setdefault(ancestors, []).append(other)

        if not placements:
            return hierarchy
        if len(placements) > 1:
            options = '; '.join(
                f'{" > ".join(ancestors + (top,))} by '
                f'{", ".join(map(label, others))}'
                for ancestors, others in placements.items()
            )
            raise OrgUnitMappingError(
                f'{where}[{index + 1}] starts targetOrgUnitHierarchy at {top!r}, '
                f'which rules place in more than one position ({options}) — '
                'list the full chain from the top level'
            )

        (ancestors, others), = placements.items()
        log.info(
            '%s[%d] targetOrgUnitHierarchy %s completed to %s from %s',
            where, index + 1, list(hierarchy), list(ancestors + hierarchy),
            label(others[0]),
        )
        return ancestors + hierarchy

    return [
        replace(rule, target_org_unit_hierarchy=complete(index))
        for index, rule in enumerate(rules)
    ]


def _same_target(first: MappingRule, second: MappingRule) -> bool:
    """True when two rules name the same org unit at the same written position."""
    return (
        first.target_org_unit_name == second.target_org_unit_name
        and first.target_org_unit_hierarchy == second.target_org_unit_hierarchy
    )


def _starts_with(names: tuple[str, ...], prefix: tuple[str, ...]) -> bool:
    return names[:len(prefix)] == prefix


def _parse_rule(raw_rule, where: str) -> MappingRule:
    if not isinstance(raw_rule, dict):
        raise OrgUnitMappingError(f'{where} must be a mapping')

    selectors = [key for key in (ID_SELECTOR, NAME_SELECTOR) if _text(raw_rule.get(key))]
    if len(selectors) != 1:
        raise OrgUnitMappingError(
            f'{where} sets {" and ".join(selectors) or "neither"} — exactly one of '
            f'{ID_SELECTOR} or {NAME_SELECTOR} is required'
        )

    target_org_unit_id = _optional_int(raw_rule, TARGET_ID, where)
    target_org_unit_name = _text(raw_rule.get(TARGET_NAME))
    target_org_unit_hierarchy = _parse_hierarchy(raw_rule, where)
    if (
        target_org_unit_id is None
        and target_org_unit_name is None
        and target_org_unit_hierarchy is None
    ):
        raise OrgUnitMappingError(
            f'{where} sets none of {TARGET_ID}, {TARGET_NAME} or {TARGET_HIERARCHY} '
            '— at least one is required'
        )

    return MappingRule(
        source_org_unit_id=_optional_int(raw_rule, ID_SELECTOR, where),
        source_org_unit_name=_text(raw_rule.get(NAME_SELECTOR)),
        target_org_unit_id=target_org_unit_id,
        target_org_unit_name=target_org_unit_name,
        target_org_unit_hierarchy=target_org_unit_hierarchy,
    )


def _parse_hierarchy(raw_rule: dict, where: str) -> tuple[str, ...] | None:
    raw_hierarchy = raw_rule.get(TARGET_HIERARCHY)
    if raw_hierarchy is None:
        return None
    if not isinstance(raw_hierarchy, list):
        raise OrgUnitMappingError(
            f'{where} {TARGET_HIERARCHY} must be a list of names, top first'
        )

    hierarchy = tuple(_text(ancestor) for ancestor in raw_hierarchy)
    if None in hierarchy:
        raise OrgUnitMappingError(f'{where} has a blank entry in {TARGET_HIERARCHY}')
    return hierarchy


def _text(value) -> str | None:
    """Normalise a scalar YAML value to non-empty stripped text, or None."""
    if value is None:
        return None
    return str(value).strip() or None


def _optional_int(raw_rule: dict, key: str, where: str) -> int | None:
    """Read a numeric field.  Org unit ids are integers, so "008" reads as 8."""
    text = _text(raw_rule.get(key))
    if text is None:
        return None
    try:
        return int(text)
    except ValueError:
        raise OrgUnitMappingError(f'{where} has a non-numeric {key} {text!r}') from None
