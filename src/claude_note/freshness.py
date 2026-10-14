"""Local saved/index generations; no network and no note text in the manifest.

An indexer must capture a saved generation before indexing and acknowledge only
that generation after success. Work arriving during indexing remains dirty.
Pending mutations stay dirty after a crash until their intent is reconciled.
"""
import math
import re
import time

from . import mutation


def _empty():
    return {'version': 1, 'generation': 0, 'saved_generation': 0,
            'lexical_generation': 0, 'vector_generation': 0, 'index_owner': None,
            'index_baseline_generation': 0, 'lexical_proof_scope': None, 'vector_proof_scope': None,
            'retained_revisions': {}, 'dirty': {}}


def snapshot(vault_root=None):
    root = mutation._vault(vault_root)
    value = mutation._metadata(root, 'freshness', 'manifest') or _empty()
    if value.get('version') != 1 or not isinstance(value.get('dirty'), dict):
        raise ValueError('Invalid freshness manifest')
    owner = value.get('index_owner')
    if owner is not None and (not isinstance(owner, str) or not re.fullmatch(r'[a-f0-9]{64}', owner)):
        raise ValueError('Invalid freshness index owner')
    baseline = value.get('index_baseline_generation', 0)
    if type(baseline) is not int or not 0 <= baseline <= value.get('saved_generation', -1):
        raise ValueError('Invalid freshness index baseline')
    for capability in ('lexical', 'vector'):
        scope = value.get(capability + '_proof_scope')
        if scope is not None and (not isinstance(scope, str) or not re.fullmatch(r'[a-f0-9]{64}', scope)):
            raise ValueError('Invalid freshness proof scope')
    retained = value.get('retained_revisions', value['dirty'])
    if not isinstance(retained, dict):
        raise ValueError('Invalid retained freshness revisions')
    return value


def _change(path, op_id, digest=None, *, vault_root=None):
    root = mutation._vault(vault_root)
    target = mutation.resolve_note_path(path, mutation._configured_root(vault_root), allow_protected=True)
    relative = target.relative_to(root).as_posix()
    # The derived index is not corpus content. Avoid recursive dirtying when its
    # own cache is saved through the confined writer.
    if relative.startswith('.claude-note/') or target.suffix.lower() != '.md':
        return snapshot(root)
    lock_path = root / '.claude-note/mutations/freshness/manifest.json'
    with mutation.note_lock(lock_path, vault_root=root):
        value = snapshot(root)
        old = value['dirty'].get(relative, {})
        if old.get('operation_id') == op_id:
            generation = old['generation']
        else:
            generation = value['generation'] + 1
            value['generation'] = generation
        value['dirty'][relative] = {'generation': generation, 'operation_id': op_id,
                                    'status': 'saved' if digest is not None else 'pending',
                                    'revision': digest}
        retained = value.setdefault('retained_revisions', dict(value['dirty']))
        retained[relative] = dict(value['dirty'][relative])
        if digest is not None:
            value['saved_generation'] = max(value['saved_generation'], generation)
        mutation._metadata(root, 'freshness', 'manifest', value)
        return value


def mark_pending(path, op_id, *, vault_root=None):
    return _change(path, op_id, vault_root=vault_root)


def mark_saved(path, digest, op_id, *, vault_root=None):
    return _change(path, op_id, digest, vault_root=vault_root)


def acknowledge(generation, *, capability='lexical', vault_root=None, index_owner=None, deadline=None,
                proof_scope=None):
    if capability not in ('lexical', 'vector'):
        raise ValueError('Unknown index capability')
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError('Invalid generation')
    if index_owner is not None and (not isinstance(index_owner, str)
                                   or not re.fullmatch(r'[a-f0-9]{64}', index_owner)):
        raise ValueError('Invalid index owner')
    if deadline is not None and (type(deadline) not in (int, float) or not math.isfinite(deadline)):
        raise ValueError('Invalid acknowledgement deadline')
    if proof_scope is not None and (not isinstance(proof_scope, str) or not re.fullmatch(r'[a-f0-9]{64}', proof_scope)):
        raise ValueError('Invalid acknowledgement proof scope')
    if index_owner is not None and proof_scope is None:
        raise ValueError('Index-owned acknowledgement requires its proof scope')
    if proof_scope is not None and index_owner is None:
        raise ValueError('Proof scope requires an index owner')
    root = mutation._vault(vault_root)
    with mutation.note_lock(root / '.claude-note/mutations/freshness/manifest.json', vault_root=root):
        value = snapshot(root)
        if deadline is not None and time.time() >= deadline:
            raise ValueError('Index freshness acknowledgement deadline expired')
        if generation > value['saved_generation']:
            raise ValueError('Cannot acknowledge an unsaved generation')
        current_owner = value.get('index_owner')
        if current_owner is not None and current_owner != index_owner:
            raise ValueError('Freshness journal belongs to another index owner')
        if current_owner is None and index_owner is not None:
            # Legacy counters have no index identity. Binding one successful
            # capability must not inherit another index's old capability.
            value['index_baseline_generation'] = min(value['lexical_generation'], value['vector_generation'],
                                                     value['saved_generation'])
            value['lexical_generation'] = 0
            value['vector_generation'] = 0
            value['lexical_proof_scope'] = None
            value['vector_proof_scope'] = None
            value['index_owner'] = index_owner
        if index_owner is not None:
            retained = value.setdefault('retained_revisions', {})
            retained.update({path: dict(entry) for path, entry in value['dirty'].items()})
            if value.get(capability + '_proof_scope') != proof_scope:
                value[capability + '_generation'] = 0
            value[capability + '_proof_scope'] = proof_scope
        value[capability + '_generation'] = max(value[capability + '_generation'], generation)
        complete = min(value['lexical_generation'], value['vector_generation'])
        if index_owner is not None and value.get('lexical_proof_scope') != value.get('vector_proof_scope'):
            complete = 0
        value['dirty'] = {path: entry for path, entry in value['dirty'].items()
                          if entry['generation'] > complete or entry['status'] != 'saved'}
        mutation._metadata(root, 'freshness', 'manifest', value)
        return value
