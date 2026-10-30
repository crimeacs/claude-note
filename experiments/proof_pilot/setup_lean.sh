#!/usr/bin/env bash
# CI-only source/bootstrap. Installs nothing: caller supplies pinned elan/Lean.
# Open Ontologies is MIT licensed; preserve its LICENSE in the sparse checkout.
set -euo pipefail
[[ "${GITHUB_ACTIONS:-}" == "true" ]] || { echo 'CI-only bootstrap' >&2; exit 2; }
[[ $# -ge 1 && $# -le 2 ]] || { echo 'usage: setup_lean.sh DEST [checkout-only|build]' >&2; exit 2; }
dest="$1"
mode="${2:-build}"
[[ "$mode" == build || "$mode" == checkout-only ]] || exit 2
commit=2ecb50c2ca7e99778d767251ec8ca64146c1669f
remote=https://github.com/fabio-rovai/open-ontologies.git
toolchain=leanprover/lean4:v4.33.1
if [[ ! -e "$dest" ]]; then
  mkdir -p "$dest"
  git -C "$dest" init -q
  git -C "$dest" remote add origin "$remote"
  git -C "$dest" config remote.origin.promisor true
  git -C "$dest" config remote.origin.partialclonefilter blob:none
  git -C "$dest" sparse-checkout init --no-cone
  git -C "$dest" sparse-checkout set --no-cone '/lean/' '/python/src/open_ontologies_lite/horn/' '/LICENSE'
  timeout -k 5 120 git -C "$dest" fetch --depth=1 --filter=blob:none origin "$commit"
  timeout -k 5 120 git -C "$dest" checkout --detach FETCH_HEAD
fi
[[ "$(git -C "$dest" rev-parse HEAD)" == "$commit" ]] || exit 3
[[ "$(git -C "$dest" remote get-url origin)" == "$remote" ]] || exit 3
git -C "$dest" diff --quiet
git -C "$dest" diff --cached --quiet
[[ "$(cat "$dest/lean/lean-toolchain")" == "$toolchain" ]] || exit 3
[[ "$mode" != checkout-only ]] || { echo "Pinned source ready: $commit"; exit 0; }
command -v lake >/dev/null
dest="$(cd "$dest" && pwd)"
cd "$dest/lean"
timeout -k 5 600 lake build oo-horn
checker="$dest/lean/.lake/build/bin/oo-horn"
timeout -k 1 5 "$checker" --version
lean --version
lake --version
# This inspects dependencies of the proved algorithm's theorem, not per-case
# kernel replay. No axioms/admit/sorry are introduced by this generated input.
axiom_input="$dest/lean/PilotAxiomInspection.lean"
[[ ! -e "$axiom_input" ]] || exit 3
printf '%s\n' 'import OOCert.Horn' '#print axioms OOCert.horn_certificate_sound' > "$axiom_input"
timeout -k 2 60 lake env lean "$axiom_input"
python3 - "$dest" "$checker" "$commit" "$toolchain" <<'PY'
import hashlib,json,pathlib,sys
root,checker,commit,toolchain=map(str,sys.argv[1:])
root,checker=pathlib.Path(root),pathlib.Path(checker)
print(json.dumps({"upstream_commit":commit,"lean_toolchain":toolchain,
 "checker_path":str(checker),"checker_sha256":hashlib.sha256(checker.read_bytes()).hexdigest(),
 "pyoxigraph_version":"0.5.11","upstream_license":"MIT",
 "lean_source_sha256":{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                       for p in sorted((root/'lean').rglob('*.lean'))}},sort_keys=True))
PY
