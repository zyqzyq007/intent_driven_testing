"""
Step 1: Input Transformation
============================
Scans a Java project and extracts Focal Method ↔ Test Case pairs.

Output schema (one item per focal method):
{
    "test_class":      str,
    "focal_class":     str,
    "focal_method":    str,
    "test_file_path":  str,
    "focal_file_path": str,
    "focal_code":      str,
    "test_imports":    list[str],
    "test_methods": [
        {
            "test_method": str,
            "test_code":   str
        }, ...
    ]
}

"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import javalang

from pipeline.utils import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _all_java_files(root: Path) -> List[Path]:
    return list(root.rglob("*.java"))


def _parse_file(path: Path) -> Tuple[Optional[object], str]:
    """Returns (javalang CompilationUnit | None, raw_content)."""
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
        return javalang.parse.parse(content), content
    except Exception as exc:
        logger.debug("Parse failed for %s: %s", path, exc)
        return None, ""


def _extract_method_source(content: str, method_node) -> str:
    """
    Extracts source lines for a method by counting braces from its start line.
    javalang gives 1-based line positions.
    """
    if not method_node.position:
        return ""
    start = method_node.position.line - 1          # 0-based index
    lines = content.splitlines()
    depth = 0
    started = False
    collected: List[str] = []
    for line in lines[start:]:
        collected.append(line)
        for ch in line:
            if ch == "{":
                depth += 1
                started = True
            elif ch == "}":
                depth -= 1
        if started and depth == 0:
            break
    return "\n".join(collected)


def _is_test_class(class_decl) -> bool:
    name = class_decl.name
    if name.endswith("Test") or name.endswith("Tests"):
        return True
    for method in class_decl.methods:
        if method.annotations:
            for ann in method.annotations:
                if ann.name in ("Test", "ParameterizedTest"):
                    return True
    return False


def _is_test_method(method) -> bool:
    if method.annotations:
        for ann in method.annotations:
            if ann.name in ("Test", "ParameterizedTest"):
                return True
    return method.name.lower().startswith("test")


def _guess_focal_method(test_method_name: str, focal_methods: Dict[str, object]) -> Optional[str]:
    """
    Matches a test method name to the most likely focal method.

    Strategy (in priority order):
    1. Exact match after stripping 'test' prefix  (testEncode → encode)
    2. Case-insensitive substring match            (testEncodeBase64 → encode)
    3. Longest common subsequence among names      (fallback)
    """
    stripped = test_method_name
    for prefix in ("test_", "test"):
        if stripped.lower().startswith(prefix):
            stripped = stripped[len(prefix):]
            break

    # 1. Exact (case-insensitive)
    for name in focal_methods:
        if name.lower() == stripped.lower():
            return name

    # 2. Substring
    best: Optional[str] = None
    best_len = 0
    for name in focal_methods:
        low_name = name.lower()
        low_stripped = stripped.lower()
        if low_name in low_stripped or low_stripped in low_name:
            if len(name) > best_len:
                best = name
                best_len = len(name)
    if best:
        return best

    return None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class JavaProjectScanner:
    """
    Scans a Maven-style Java project and builds an index of all classes/methods.

    Separates main sources (src/main/java) from test sources (src/test/java).
    Falls back to scanning the whole tree if the standard layout is absent.
    """

    def __init__(self, project_root: Path):
        self.project_root = project_root
        self._main_classes: Dict[str, Dict] = {}   # class_name → info
        self._test_classes: Dict[str, Dict] = {}   # class_name → info

    # ------------------------------------------------------------------
    def scan(self) -> None:
        """Scans the project and populates internal class maps."""
        main_root, test_root = self._locate_source_roots()
        logger.info("Main sources: %s", main_root)
        logger.info("Test sources: %s", test_root)

        main_files = _all_java_files(main_root)
        test_files = _all_java_files(test_root)

        logger.info("Found %d main files, %d test files", len(main_files), len(test_files))

        for path in main_files:
            self._index_file(path, is_test=False)
        for path in test_files:
            self._index_file(path, is_test=True)

        logger.info(
            "Indexed %d main classes, %d test classes",
            len(self._main_classes),
            len(self._test_classes),
        )

    # ------------------------------------------------------------------
    def extract_pairs(self) -> List[Dict]:
        """
        Matches test methods to focal methods and groups them by (test_class, focal_method).
        """
        grouped_pairs: Dict[Tuple[str, str], Dict] = {}

        for test_class_name, test_info in self._test_classes.items():
            # Guess the focal class name by stripping Test/Tests suffix
            focal_name = (
                test_class_name.removesuffix("Tests").removesuffix("Test")
            )
            focal_info = self._main_classes.get(focal_name)

            if focal_info is None:
                logger.debug("No focal class found for test class '%s'", test_class_name)
                continue

            for method_name, method_ast in test_info["methods"].items():
                if not _is_test_method(method_ast):
                    continue

                focal_method_name = _guess_focal_method(method_name, focal_info["methods"])
                if focal_method_name is None:
                    logger.debug(
                        "  Could not map test method '%s.%s' to any focal method",
                        test_class_name,
                        method_name,
                    )
                    continue

                test_code = _extract_method_source(test_info["content"], method_ast)
                focal_method_ast = focal_info["methods"][focal_method_name]
                focal_code = _extract_method_source(focal_info["content"], focal_method_ast)

                group_key = (test_class_name, focal_method_name)
                if group_key not in grouped_pairs:
                    grouped_pairs[group_key] = {
                        "test_class": test_class_name,
                        "focal_class": focal_name,
                        "focal_method": focal_method_name,
                        "test_file_path": str(test_info["file_path"]),
                        "focal_file_path": str(focal_info["file_path"]),
                        "focal_code": focal_code,
                        "test_imports": test_info.get("imports", []),
                        "test_methods": []
                    }

                grouped_pairs[group_key]["test_methods"].append({
                    "test_method": method_name,
                    "test_code": test_code
                })

        return list(grouped_pairs.values())

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _locate_source_roots(self) -> Tuple[Path, Path]:
        """
        Returns (main_root, test_root).
        Uses Maven standard layout if present, otherwise uses project root for both.
        """
        main_candidate = self.project_root / "src" / "main" / "java"
        test_candidate = self.project_root / "src" / "test" / "java"
        main_root = main_candidate if main_candidate.is_dir() else self.project_root
        test_root = test_candidate if test_candidate.is_dir() else self.project_root
        return main_root, test_root

    def _index_file(self, path: Path, *, is_test: bool) -> None:
        tree, content = _parse_file(path)
        if tree is None:
            return

        for _, class_decl in tree.filter(javalang.tree.ClassDeclaration):
            # For test files only index test classes; for main files only non-test classes
            if is_test and not _is_test_class(class_decl):
                continue
            if not is_test and _is_test_class(class_decl):
                continue

            methods: Dict[str, object] = {}
            for method in class_decl.methods:
                # Keep all methods; test-vs-focal filtering happens at pair extraction
                methods[method.name] = method

            target = self._test_classes if is_test else self._main_classes
            
            # Extract package and imports
            imports = []
            if tree.package:
                imports.append(f"package {tree.package.name};")
                
            if tree.imports:
                for imp in tree.imports:
                    import_str = "import "
                    if imp.static:
                        import_str += "static "
                    import_str += imp.path
                    if imp.wildcard:
                        import_str += ".*"
                    import_str += ";"
                    imports.append(import_str)

            info = {
                "name": class_decl.name,
                "file_path": path,
                "content": content,
                "methods": methods,
                "imports": imports,
            }

            target[class_decl.name] = info


# ---------------------------------------------------------------------------
# Standalone runner
# ---------------------------------------------------------------------------

def run(project_path: Path, output_path: Path) -> List[Dict]:
    """
    Full Step-1 pipeline: scan → extract → save.

    Returns the list of pairs so callers can inspect results without re-loading.
    """
    logger.info("=== Step 1: Input Transformation ===")
    logger.info("Project: %s", project_path)
    logger.info("Output:  %s", output_path)

    scanner = JavaProjectScanner(project_path)
    scanner.scan()
    pairs = scanner.extract_pairs()

    logger.info("Extracted %d focal-test pairs", len(pairs))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(pairs, fh, indent=2, ensure_ascii=False)

    logger.info("Pairs saved → %s", output_path)
    return pairs
