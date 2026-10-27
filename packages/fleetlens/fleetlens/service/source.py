"""Reading the source behind an indexed symbol.

Every other service here answers from the index. This one goes back to the repository, which
makes it the only place where the answer can disagree with the index that pointed at it.

So it reads at the commit the index was built from rather than from the working copy. A
server that rebuilds on a timer pulls new commits between builds, and a span recorded as
lines 42 to 58 may hold something else by the time anyone asks. Returning whatever occupies
those lines today would be confidently wrong, and wrong in the one way this project exists
to avoid: indistinguishable from a fact. It also keeps a pinned index honest, since a pin
whose source came from the working tree would answer an old graph with today's code, exactly
when somebody is depending on it not to.

Reading at a commit is only possible because the index already records one per repository
(`SqliteStore.repo_shas`). Where that commit is no longer reachable, because it was garbage
collected or the branch was force-pushed, the working copy is used and the answer says so.
"""
from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any, Optional

NODE_TYPE = "code_symbol"

#: Hard ceiling on returned lines, whatever the caller asks for. fleetlens is measured on
#: getting an agent to an answer in fewer tokens, and the mechanism is that it stops opening
#: files. A tool that will hand back a thousand lines invites it straight back.
MAX_LINES_CEILING = 500

#: Lines of context either side of the recorded span. Small on purpose: the span is the
#: answer, and the context is for a decorator or a signature that sits just outside it.
MAX_CONTEXT_LINES = 50

_GIT_TIMEOUT = 10


def _git(args: list[str], cwd: Path) -> Optional[str]:
    """stdout of a git command, or None for any failure at all.

    Never raises. A missing git binary, a directory that is not a checkout and a commit that
    no longer exists are all the same thing here: this read cannot be served from history,
    and the caller falls back and says so.
    """
    out = _git_bytes(args, cwd)
    return None if out is None else out.decode("utf-8", "replace")


def _git_bytes(args: list[str], cwd: Path) -> Optional[bytes]:
    """As `_git`, without decoding.

    File contents are compared against a hash of the bytes the indexer read, so they have
    to stay bytes until that comparison is done. Decoding first would be enough to break
    it: `text=True` applies universal newlines, so every CRLF file would hash differently
    here than it did at index time and be reported as drifted when nothing had moved.
    """
    try:
        res = subprocess.run(["git", "-C", str(cwd), *args],
                             capture_output=True, timeout=_GIT_TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return None
    return res.stdout if res.returncode == 0 else None


class SourceService:
    def __init__(self, knowledge, store):
        self.knowledge = knowledge
        self.store = store

    def _not_found(self, given: str) -> dict[str, Any]:
        resp: dict[str, Any] = {"status": "not_found", "id": given,
                                "expected_format": "code_symbol:<slug>:<path>::<qualname>"}
        hits = self.knowledge.find_ids(given.split(":")[-1], NODE_TYPE, limit=5)
        if hits:
            resp["did_you_mean"] = hits
        return resp

    def _repo_for(self, slug: str) -> Optional[tuple[Path, Path, Optional[str]]]:
        """(checkout root, service dir relative to it, indexed commit) for a service slug.

        The service payload records where the service lived, which for a repository holding
        several services is a subdirectory. Commits are recorded per *checkout*, so the
        checkout root has to be found before the sha can be looked up, and the symbol's path
        has to be rebased onto it before git will recognise it.
        """
        svc = self.knowledge.get(f"service:{slug}")
        if svc is None:
            return None
        root = str((svc.payload or {}).get("root") or "")
        if not root or not Path(root).is_dir():
            return None
        service_dir = Path(root)
        top = _git(["rev-parse", "--show-toplevel"], service_dir)
        if not top:
            # Not a checkout. Still readable from disk, just never from history.
            return service_dir, Path("."), None
        checkout = Path(top.strip())
        try:
            prefix = service_dir.resolve().relative_to(checkout.resolve())
        except ValueError:
            prefix = Path(".")
        return checkout, prefix, self.store.repo_shas().get(checkout.name)

    def _best_text(self, checkout: Path, in_repo: str, sha: Optional[str],
                   want: str) -> tuple[Optional[str], str, bool]:
        """(text, where it came from, whether it matches what was indexed).

        Tries the recorded commit first, because that is what the span was supposed to
        have been measured against, then the working copy. Either can be the right answer:
        a file edited after indexing is correct in history and wrong on disk, while a file
        that was uncommitted when it was parsed is the reverse. Rather than pick a side and
        be silently wrong half the time, read both and let the hash decide.

        With no recorded hash, from an index built before they existed, the first readable
        source wins and the caller is told it could not be checked.
        """
        candidates: list[tuple[str, Optional[bytes]]] = []
        if sha:
            candidates.append(("commit", _git_bytes(["show", f"{sha}:{in_repo}"], checkout)))
        try:
            candidates.append(("working tree", (checkout / in_repo).read_bytes()))
        except OSError:
            pass

        readable = [(where, raw) for where, raw in candidates if raw is not None]
        if not readable:
            return None, "", False
        if want:
            for where, raw in readable:
                if hashlib.sha1(raw).hexdigest()[:12] == want:
                    return raw.decode("utf-8", "replace"), where, True
        where, raw = readable[0]
        return raw.decode("utf-8", "replace"), where, False

    def get_source(self, symbol_id: str, context_lines: int = 0,
                   max_lines: int = 200) -> dict[str, Any]:
        obj = self.knowledge.get(symbol_id)
        if obj is None or obj.object_type != NODE_TYPE:
            return self._not_found(symbol_id)

        p = obj.payload or {}
        rel, start, end = p.get("path"), p.get("line_start"), p.get("line_end")
        if not rel or not isinstance(start, int) or not isinstance(end, int):
            return {"status": "unavailable", "id": symbol_id,
                    "reason": "this symbol carries no file span"}

        slug = symbol_id.split(":", 2)[1] if symbol_id.startswith(f"{NODE_TYPE}:") \
            else symbol_id.split(":", 1)[0]
        located = self._repo_for(slug)
        if located is None:
            return {"status": "unavailable", "id": symbol_id,
                    "reason": f"the checkout for '{slug}' is not on this machine",
                    "hint": "an index outlives the repositories it was built from; "
                            "re-index, or serve from the machine that holds them"}
        checkout, prefix, sha = located
        in_repo = (prefix / rel).as_posix() if str(prefix) != "." else rel

        want = p.get("file_sha") or ""
        text, read_from, verified = self._best_text(checkout, in_repo, sha, want)
        if text is None:
            return {"status": "unavailable", "id": symbol_id,
                    "reason": f"could not read {in_repo}"}

        if want and not verified:
            # Both the recorded commit and the working copy disagree with the file these
            # line numbers were counted in. The span may still land on real, valid code,
            # which is exactly why this cannot be returned: a shifted span returns a
            # different function with nothing to suggest anything is wrong. The index is
            # what is stale, so say that rather than guessing at an offset.
            return {"status": "span_mismatch", "id": symbol_id, "path": in_repo,
                    "line_start": start, "line_end": end,
                    "reason": "the file has changed since these line numbers were "
                              "recorded, and neither the indexed commit nor the working "
                              "copy matches what was parsed",
                    "hint": f"re-index this repository; the span for {p.get('qualname')} "
                            f"cannot be trusted until you do"}

        lines = text.splitlines()
        pad = max(0, min(int(context_lines or 0), MAX_CONTEXT_LINES))
        cap = max(1, min(int(max_lines or 1), MAX_LINES_CEILING))

        lo = max(1, start - pad)
        hi = min(len(lines), end + pad)
        truncated = (hi - lo + 1) > cap
        if truncated:
            hi = lo + cap - 1

        out: dict[str, Any] = {"status": "ok", "id": symbol_id, "path": in_repo,
                               "line_start": lo, "line_end": hi}
        if want and verified:
            pass
        else:
            # An index from before per-file hashes. The lines may be right and there is no
            # way to tell, so say it in words as well as in a flag: a reader skimming for
            # `status` would otherwise take this for a checked answer.
            out["warning"] = ("this index records no file hash, so the span could not be "
                              "checked against the file it was measured in; re-index to "
                              "make this verifiable")
        out.update({
            "lang": p.get("lang"), "qualname": p.get("qualname"),
            "read_from": read_from,
            "commit": sha if read_from == "commit" else None,
            # True only when the bytes read hash to what the indexer parsed. False means
            # the index predates per-file hashes and the span is unverifiable, which is a
            # weaker claim than it looks and is said rather than implied.
            "verified": bool(want) and verified,
            "truncated": truncated,
            "source": "\n".join(lines[lo - 1:hi]),
        })
        return out
