"""Local saved/index generations; no network and no note text in the manifest.

An indexer must capture a saved generation before indexing and acknowledge only
that generation after success. Work arriving during indexing remains dirty.
Pending mutations stay dirty after a crash until their intent is reconciled.
"""
from . import mutation


def _empty():
    return {'version': 1, 'generation': 0, 'saved_generation': 0,
            'lexical_generation': 0, 'vector_generation': 0, 'dirty': {}}


def snapshot(vault_root=None):
    root = mutation._vault(vault_root)
    value = mutation._metadata(root, 'freshness', 'manifest') or _empty()
    if value.get('version') != 1 or not isinstance(value.get('dirty'), dict):
        raise ValueError('Invalid freshness manifest')
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
        if digest is not None:
            value['saved_generation'] = max(value['saved_generation'], generation)
        mutation._metadata(root, 'freshness', 'manifest', value)
        return value


def mark_pending(path, op_id, *, vault_root=None):
    return _change(path, op_id, vault_root=vault_root)


def mark_saved(path, digest, op_id, *, vault_root=None):
    return _change(path, op_id, digest, vault_root=vault_root)


def acknowledge(generation, *, capability='lexical', vault_root=None):
    if capability not in ('lexical', 'vector'):
        raise ValueError('Unknown index capability')
    if not isinstance(generation, int) or isinstance(generation, bool) or generation < 0:
        raise ValueError('Invalid generation')
    root = mutation._vault(vault_root)
    with mutation.note_lock(root / '.claude-note/mutations/freshness/manifest.json', vault_root=root):
        value = snapshot(root)
        if generation > value['saved_generation']:
            raise ValueError('Cannot acknowledge an unsaved generation')
        value[capability + '_generation'] = max(value[capability + '_generation'], generation)
        complete = min(value['lexical_generation'], value['vector_generation'])
        value['dirty'] = {path: entry for path, entry in value['dirty'].items()
                          if entry['generation'] > complete or entry['status'] != 'saved'}
        mutation._metadata(root, 'freshness', 'manifest', value)
        return value
