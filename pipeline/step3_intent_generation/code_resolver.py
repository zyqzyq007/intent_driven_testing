"""
Method & Field Code Resolver
=============================
Resolves source-code snippets for methods and fields referenced in the
BehavioralSemanticSlice, so that IntentRecord.context_code is self-contained.

Two lookup strategies (tried in order):
  1. pairs_index  — the 189 focal methods already extracted in pairs.json.
                    Fast and exact (brace-balanced extraction was already done).
  2. source_scan  — walk all .java files under the raw project root,
                    parse with a brace-balanced extractor, cache results.
                    Covers methods NOT in pairs.json (e.g. isRunningFromServlet).

Field definitions are resolved by scanning the focal class file for lines
matching "Type fieldName" declarations (field/member variable style).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional

from pipeline.utils import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Brace-balanced method extractor (same approach as step1 extractor)
# ---------------------------------------------------------------------------

def _extract_method_by_name(source: str, method_name: str) -> Optional[str]:
    """
    Extracts the full source of the first method named `method_name` found
    in `source`, using brace-counting to capture the complete body.

    Returns None if not found.
    """
    # Match any access modifier combination + return type + method name + (
    # We allow method_name to appear as word boundary so "get" won't match "getValue"
    pattern = re.compile(
        r'(?:(?:public|private|protected|static|final|synchronized|native|abstract)'
        r'\s+)*'                          # modifiers (0 or more)
        r'[\w<>\[\],\s]+\s+'             # return type
        r'\b' + re.escape(method_name) + r'\b'
        r'\s*\([^)]*\)'                  # parameter list  (simple, no nested parens)
        r'(?:\s+throws\s+[\w\s,]+)?'     # optional throws
        r'\s*\{',                         # opening brace
        re.MULTILINE,
    )

    for match in pattern.finditer(source):
        start = match.start()
        brace_pos = match.end() - 1   # position of '{'
        depth = 0
        i = brace_pos
        while i < len(source):
            ch = source[i]
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return source[start: i + 1].strip()
            i += 1

    return None


def _extract_imports(source: str) -> str:
    """Extracts all import statements from a Java source file."""
    lines = []
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith("import ") or stripped.startswith("package "):
            lines.append(stripped)
        elif lines and not stripped:
            continue   # allow blank lines between imports
        elif lines:
            break       # stop at first non-import/non-blank line after imports started
    return "\n".join(lines)


def _extract_field_definition(source: str, field_name: str) -> Optional[str]:
    """
    Finds the line(s) in source that declare a field named `field_name`.

    Matches patterns like:
        private int port = DEFAULT_PORT;
        protected volatile boolean initialized = false;
        private String embeddedServerIdentifier;
    Returns the trimmed declaration line, or None.
    """
    pattern = re.compile(
        r'^[\s]*(?:(?:private|protected|public|static|final|volatile|transient)\s+)*'
        r'[\w<>\[\],\s]+\s+\b' + re.escape(field_name) + r'\b'
        r'[^;]*;',
        re.MULTILINE,
    )
    m = pattern.search(source)
    if m:
        return m.group(0).strip()
    return None


# ---------------------------------------------------------------------------
# Source file index
# ---------------------------------------------------------------------------

class _SourceIndex:
    """
    Lazily indexes all .java files under `project_root/src/main/java`.
    Maps  simple_class_name → list of Path  (multiple classes can share a name).
    """

    def __init__(self, project_root: Path):
        self._root = project_root / "src" / "main" / "java"
        self._class_to_files: Dict[str, List[Path]] = {}
        self._file_source_cache: Dict[Path, str] = {}
        self._indexed = False

    def _ensure_indexed(self) -> None:
        if self._indexed:
            return
        if not self._root.exists():
            logger.warning("Source root not found: %s", self._root)
            self._indexed = True
            return
        for java_file in self._root.rglob("*.java"):
            stem = java_file.stem  # e.g. "Service"
            self._class_to_files.setdefault(stem, []).append(java_file)
        logger.debug(
            "Source index built: %d unique class names from %s",
            len(self._class_to_files), self._root,
        )
        self._indexed = True

    def get_source(self, class_name: str) -> List[str]:
        """Returns list of source strings for all files matching class_name."""
        self._ensure_indexed()
        results = []
        for path in self._class_to_files.get(class_name, []):
            if path not in self._file_source_cache:
                try:
                    self._file_source_cache[path] = path.read_text(encoding="utf-8")
                except Exception as e:
                    logger.debug("Could not read %s: %s", path, e)
                    continue
            results.append(self._file_source_cache[path])
        return results

    def get_source_for_file(self, file_path: Path) -> Optional[str]:
        """Returns source for a specific file path."""
        if file_path not in self._file_source_cache:
            try:
                self._file_source_cache[file_path] = file_path.read_text(encoding="utf-8")
            except Exception as e:
                logger.debug("Could not read %s: %s", file_path, e)
                return None
        return self._file_source_cache[file_path]


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class MethodCodeResolver:
    """
    Resolves source-code snippets needed to populate ContextCode.

    Usage
    -----
        resolver = MethodCodeResolver(project_root, pairs)
        code  = resolver.get_method_code("Service", "init")
        field = resolver.get_field_definition("Service.java", "port")
        imports = resolver.get_imports("Service.java")
    """

    def __init__(self, project_root: Path, pairs: List[dict]):
        # Fast lookup from pairs.json
        self._pairs_index: Dict[str, str] = {}   # "ClassName.methodName" → focal_code
        for p in pairs:
            key = f"{p['focal_class']}.{p['focal_method']}"
            self._pairs_index[key] = p["focal_code"]

        # File-path based lookup for focal class source
        self._file_index: Dict[str, Path] = {}   # "ClassName.methodName" → file path
        for p in pairs:
            key = f"{p['focal_class']}.{p['focal_method']}"
            self._file_index[key] = Path(p["focal_file_path"])

        # Fallback: scan all source files
        self._src_index = _SourceIndex(project_root)

    # ------------------------------------------------------------------
    def get_method_code(self, class_name: str, method_name: str) -> Optional[str]:
        """
        Returns the source of a method.  Tries pairs_index first, then
        falls back to scanning source files.
        """
        key = f"{class_name}.{method_name}"

        # 1. Fast path: already in pairs index
        if key in self._pairs_index:
            return self._pairs_index[key]

        # 2. Scan source files for the class
        sources = self._src_index.get_source(class_name)
        for src in sources:
            code = _extract_method_by_name(src, method_name)
            if code:
                # Cache for future lookups
                self._pairs_index[key] = code
                return code

        logger.debug("Method not resolved: %s.%s", class_name, method_name)
        return None

    def get_field_definition(self, focal_file_path: str, field_name: str) -> Optional[str]:
        """
        Finds the field declaration for `field_name` in the focal class file.
        """
        src = self._src_index.get_source_for_file(Path(focal_file_path))
        if src is None:
            return None
        return _extract_field_definition(src, field_name)

    def get_imports(self, focal_file_path: str) -> str:
        """Returns the import block of the focal class file."""
        src = self._src_index.get_source_for_file(Path(focal_file_path))
        if src is None:
            return ""
        return _extract_imports(src)

    def resolve_context(
        self,
        focal_code:      str,
        focal_file_path: str,
        preceding_calls: list,    # List[PrecedingCall]
        downstream_calls: list,   # List[str]  (method labels)
        data_reads:      list,    # List[DataDependency]
        data_writes:     list,    # List[DataDependency]
    ) -> "ContextCode":
        """
        Convenience method: resolves everything and returns a ContextCode.
        Imports ContextCode locally to avoid circular imports at module level.
        """
        from .models import ContextCode

        # ── Related method codes ──────────────────────────────────────
        related: Dict[str, str] = {}

        for call in preceding_calls:
            label = call.method_label
            # Try to infer class from method_id  "<spark.Foo: ... bar()>"
            class_hint = _class_from_method_id(call.method_id)
            code = self.get_method_code(class_hint, label) if class_hint else None
            if code is None:
                code = self._search_by_label(label)
            if code:
                related[label] = code

        for label in downstream_calls:
            if label in related:
                continue
            code = self._search_by_label(label)
            if code:
                related[label] = code

        # ── Field definitions ─────────────────────────────────────────
        fields: Dict[str, str] = {}
        all_data = list(data_reads) + list(data_writes)
        for dep in all_data:
            fname = dep.data_label.strip()
            if fname in fields:
                continue
            defn = self.get_field_definition(focal_file_path, fname)
            if defn:
                fields[fname] = defn

        # ── Imports ───────────────────────────────────────────────────
        imports = self.get_imports(focal_file_path)

        return ContextCode(
            focal_code           = focal_code,
            related_method_codes = related,
            field_definitions    = fields,
            focal_class_imports  = imports,
        )

    # ------------------------------------------------------------------
    def _search_by_label(self, method_label: str) -> Optional[str]:
        """
        Searches all indexed source files for a method named `method_label`.
        Returns first match found.
        """
        self._src_index._ensure_indexed()
        for class_name, paths in self._src_index._class_to_files.items():
            for path in paths:
                src = self._src_index.get_source_for_file(path)
                if src is None:
                    continue
                code = _extract_method_by_name(src, method_label)
                if code:
                    cache_key = f"{class_name}.{method_label}"
                    self._pairs_index.setdefault(cache_key, code)
                    return code
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _class_from_method_id(method_id: str) -> Optional[str]:
    """
    Extracts the simple class name from an ESG method node id.
    e.g. "<spark.globalstate.ServletFlag: boolean isRunningFromServlet()>"
         → "ServletFlag"
    """
    m = re.search(r"<([^:>]+):", method_id)
    if m:
        fqn = m.group(1)          # "spark.globalstate.ServletFlag"
        return fqn.split(".")[-1] # "ServletFlag"
    return None
