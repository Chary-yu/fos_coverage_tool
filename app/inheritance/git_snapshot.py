"""Git ancestry and immutable source snapshot provider."""

from __future__ import absolute_import

import os
import re
import select
import subprocess
import time

from app.inheritance.normalizer import CppLexer


SOURCE_EXTENSIONS = (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")
# This is intentionally a recall-only lexical hint.  The C++ analyzer remains
# the authority for deciding whether a match is a function.  In particular,
# do not add a bounded parameter/signature grammar here: trailing return types,
# long parameter lists, requires clauses, and qualified/class members must all
# be able to reach the real parser.  False positives cost parser work; false
# negatives can turn a same-repository dependency into an external call.
_FUNCTION_CANDIDATE = re.compile(r"\b([A-Za-z_]\w*)\s*\(", re.MULTILINE)
_MACRO_CANDIDATE = re.compile(
    r"^\s*#\s*define\s+([A-Za-z_]\w*)", re.MULTILINE
)
_CONSTANT_DECLARATION = re.compile(
    r"\b(?:const|constexpr)\b[^;]*(?:;|$)", re.MULTILINE
)
_IDENTIFIER = re.compile(
    r"\b([A-Za-z_]\w*)\b"
)
_NON_FUNCTION_SYMBOLS = frozenset((
    "if", "for", "while", "switch", "catch", "return", "sizeof",
    "decltype", "static_cast", "dynamic_cast", "reinterpret_cast",
    "const_cast", "new", "delete",
))


class GitTechnicalFailure(RuntimeError):
    pass


class _DeadlinePipeReader(object):
    """Read a Git batch pipe with an inactivity timeout."""

    def __init__(self, stream, idle_timeout):
        self.stream = stream
        self.file_descriptor = stream.fileno()
        self.idle_timeout = max(0.0, float(idle_timeout))
        self.deadline = time.monotonic() + self.idle_timeout
        self.buffer = bytearray()

    def _fill(self):
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise GitTechnicalFailure("git cat-file batch timed out")
        try:
            readable, _, _ = select.select(
                [self.file_descriptor], [], [], remaining
            )
        except (OSError, select.error) as exc:
            raise GitTechnicalFailure(
                "git cat-file batch read failed: {}".format(exc)
            )
        if not readable:
            raise GitTechnicalFailure("git cat-file batch timed out")
        try:
            chunk = os.read(self.file_descriptor, 65536)
        except OSError as exc:
            raise GitTechnicalFailure(
                "git cat-file batch read failed: {}".format(exc)
            )
        if not chunk:
            return False
        self.buffer.extend(chunk)
        # A batch may legitimately take longer than one idle interval when it
        # keeps making progress.  Only a period with no bytes from Git is a
        # timeout; there is no fixed wall-clock cutoff for the whole batch.
        self.deadline = time.monotonic() + self.idle_timeout
        return True

    def read_line(self):
        while True:
            marker = self.buffer.find(b"\n")
            if marker >= 0:
                marker += 1
                value = bytes(self.buffer[:marker])
                del self.buffer[:marker]
                return value
            if not self._fill():
                value = bytes(self.buffer)
                self.buffer.clear()
                return value

    def read_exact(self, size):
        size = int(size)
        while len(self.buffer) < size:
            if not self._fill():
                break
        value = bytes(self.buffer[:size])
        del self.buffer[:size]
        return value


class GitSnapshotProvider(object):
    def __init__(self, repo_path, timeout=30, fetch_remote=None,
                 performance=None):
        self.repo_path = os.path.realpath(str(repo_path or ""))
        self.timeout = float(timeout)
        self.fetch_remote = fetch_remote
        self.performance = performance

    def _record_git(self, bytes_read=0):
        if self.performance is not None:
            self.performance.record_git_subprocess(bytes_read)

    def _run(self, args, check=True, strip_output=True, raw_output=False):
        output = ""
        try:
            output = subprocess.check_output(
                ["git", "-C", self.repo_path] + list(args),
                stderr=subprocess.STDOUT, timeout=self.timeout,
                universal_newlines=not raw_output,
            )
            if raw_output and isinstance(output, bytes):
                try:
                    output = output.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise GitTechnicalFailure(
                        "git source blob is not UTF-8: {}".format(exc)
                    )
            return output.strip() if strip_output else output
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            if check:
                raise GitTechnicalFailure(str(exc))
            return ""
        finally:
            if isinstance(output, bytes):
                output_bytes = len(output)
            else:
                output_bytes = len(str(output).encode("utf-8")) if output else 0
            self._record_git(output_bytes)

    def commit_available(self, commit):
        try:
            subprocess.check_call([
                "git", "-C", self.repo_path, "cat-file", "-e",
                "{}^{{commit}}".format(commit),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=self.timeout)
            return True
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            return False
        finally:
            self._record_git()

    def ensure_commit(self, commit):
        if self.commit_available(commit):
            return True
        if self.fetch_remote:
            self._run(["fetch", "--no-tags", str(self.fetch_remote), str(commit)])
        if not self.commit_available(commit):
            raise GitTechnicalFailure("required commit is unavailable: {}".format(commit))
        return True

    def is_ancestor(self, old_commit, new_commit):
        self.ensure_commit(old_commit)
        self.ensure_commit(new_commit)
        if old_commit == new_commit:
            return True
        try:
            subprocess.check_call([
                "git", "-C", self.repo_path, "merge-base", "--is-ancestor",
                str(old_commit), str(new_commit),
            ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=self.timeout)
            return True
        except subprocess.CalledProcessError:
            return False
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitTechnicalFailure(str(exc))
        finally:
            self._record_git()

    def branch(self, commit):
        return self._run(["show", "-s", "--format=%D", str(commit)], check=False)

    def read_file(self, commit, relative_path):
        relative_path = self._validate_relative_path(relative_path)
        # A source blob is not command-line output.  Preserve every byte that
        # determines physical line identity, including leading/trailing blank
        # lines and the final newline.
        return self._run(
            ["show", "{}:{}".format(commit, relative_path)],
            strip_output=False,
            raw_output=True,
        )

    @staticmethod
    def _validate_relative_path(relative_path):
        relative_path = str(relative_path)
        if (os.path.isabs(relative_path) or
                ".." in relative_path.replace("\\", "/").split("/")):
            raise ValueError("source path must be repository-relative")
        return relative_path

    def read_files(self, commit, relative_paths):
        """Yield source blobs through one persistent ``git cat-file`` process.

        ``git show COMMIT:path`` is convenient for one file but starts a
        process for every path.  The batch protocol keeps one Git subprocess
        alive while the caller still receives one decoded file at a time, so
        lexical indexing does not trade parser amplification for process
        amplification or unbounded source materialization.
        """
        paths = [self._validate_relative_path(path)
                 for path in (relative_paths or ())]
        if not paths:
            return
        process = None
        total_bytes = 0
        try:
            process = subprocess.Popen(
                ["git", "-C", self.repo_path, "cat-file", "--batch"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
            reader = _DeadlinePipeReader(
                process.stdout, self.timeout
            )
            for path in paths:
                request = "{}:{}\n".format(commit, path).encode("utf-8")
                process.stdin.write(request)
                process.stdin.flush()
                header = reader.read_line()
                if not header:
                    raise GitTechnicalFailure(
                        "git cat-file batch ended before {}".format(path)
                    )
                fields = header.rstrip(b"\n").split()
                if len(fields) >= 2 and fields[1] == b"missing":
                    raise GitTechnicalFailure(
                        "source blob is unavailable: {}".format(path)
                    )
                if len(fields) != 3 or fields[1] != b"blob":
                    raise GitTechnicalFailure(
                        "unexpected git object for source: {}".format(path)
                    )
                try:
                    size = int(fields[2])
                except (TypeError, ValueError):
                    raise GitTechnicalFailure(
                        "invalid git blob size for source: {}".format(path)
                    )
                data = reader.read_exact(size)
                separator = reader.read_exact(1)
                if len(data) != size or separator != b"\n":
                    raise GitTechnicalFailure(
                        "truncated git source blob: {}".format(path)
                    )
                total_bytes += size
                try:
                    text = data.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise GitTechnicalFailure(
                        "source blob is not UTF-8: {} ({})".format(path, exc)
                    )
                # Keep the Git blob byte-for-byte in text form.  The parser,
                # diff hunk mapper, and database line numbers all use the
                # physical lines represented by this string.
                yield path, text
            try:
                process.stdin.close()
            except (OSError, ValueError):
                pass
            try:
                return_code = process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                process.kill()
                process.wait()
                raise GitTechnicalFailure(
                    "git cat-file batch timed out"
                ) from exc
            if return_code:
                error = process.stderr.read().decode("utf-8", "replace").strip()
                raise GitTechnicalFailure(
                    "git cat-file batch failed: {}".format(error or return_code)
                )
        except BaseException:
            if process is not None and process.poll() is None:
                process.kill()
            raise
        finally:
            if process is not None:
                try:
                    if process.stdin and not process.stdin.closed:
                        process.stdin.close()
                except (OSError, ValueError):
                    pass
                if process.poll() is None:
                    process.kill()
                try:
                    process.wait(timeout=self.timeout)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                for stream in (process.stdout, process.stderr):
                    try:
                        if stream and not stream.closed:
                            stream.close()
                    except (OSError, ValueError):
                        pass
            self._record_git(total_bytes)

    def list_source_files(self, commit):
        """List the repository C/C++ universe for dependency resolution."""
        output = self._run(["ls-tree", "-r", "--name-only", str(commit)])
        paths = []
        for value in output.splitlines():
            path = value.strip().replace("\\", "/")
            if (not path or os.path.isabs(path) or
                    ".." in path.split("/") or
                    not path.lower().endswith(SOURCE_EXTENSIONS)):
                continue
            paths.append(path)
        return sorted(set(paths))

    def build_symbol_candidate_index(self, commit, paths=None):
        """Build one lightweight symbol-to-path index for a commit.

        This deliberately does not invoke the C++ parser.  It performs one
        bounded lexical pass over the source universe, so a missing ordinary
        token is answered from a negative cache instead of reparsing every
        source file for every token.
        """
        candidates = {"functions": {}, "macros": {}, "constants": {}}
        source_paths = list(paths or self.list_source_files(commit))

        def add(kind, symbol, path):
            symbol = str(symbol or "")
            if not symbol or symbol in _NON_FUNCTION_SYMBOLS:
                return
            candidates[kind].setdefault(symbol, set()).add(str(path))

        for path, text in self.read_files(commit, source_paths):
            # All three pre-indexes operate on the same translation-phase
            # logical source.  They are recall-only hints; CppSourceAnalyzer
            # remains the authority.  Keeping this shared and deliberately
            # broad is what makes line-spliced declarations and definitions
            # impossible to lose in a lexical shortcut.
            logical_lines = CppLexer._logical_lines(
                str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")
            )
            logical_source = "\n".join(
                logical_line for _, _, logical_line in logical_lines
            )
            for match in _FUNCTION_CANDIDATE.finditer(logical_source):
                add("functions", match.group(1), path)
            for _, _, logical_line in logical_lines:
                for match in _MACRO_CANDIDATE.finditer(logical_line):
                    add("macros", match.group(1), path)
                for declaration in _CONSTANT_DECLARATION.finditer(logical_line):
                    # Recall every identifier in a const declaration.  The
                    # canonical parser decides which one is the variable;
                    # broad recall is deliberate because a lexical shortcut
                    # must not invent a false negative for qualified types,
                    # line splices, or long declarations.
                    for identifier in _IDENTIFIER.finditer(declaration.group(0)):
                        add("constants", identifier.group(1), path)
        return {
            kind: {name: sorted(paths) for name, paths in values.items()}
            for kind, values in candidates.items()
        }
