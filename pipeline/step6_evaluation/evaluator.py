import json
import logging
import os
import re
import csv
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Tuple

from codebleu import calc_codebleu

from pipeline.utils import get_logger

logger = get_logger("step6_evaluator")

# ==========================================
# 1. Pipeline Execution Entry
# ==========================================
def run(execution_results_path: Path, pairs_path: Path, output_path: Path, project_root: Path) -> Dict[str, Any]:
    """
    Main entry point for Step 6, called by run_pipeline.py.
    """
    if not execution_results_path.exists():
        logger.error(f"Execution results not found: {execution_results_path}")
        return None

    exec_results = load_data(execution_results_path)
    pairs        = load_pairs(pairs_path)

    # 1) Align data
    aligned = align_predictions(exec_results, pairs)

    # 2) Classify execution results
    stats = classify_results(exec_results)

    # 3) Compute metrics
    codebleu = compute_codebleu(aligned)

    jacoco_csv   = project_root / "target" / "site" / "jacoco" / "jacoco.csv"

    # Extract generated test class names to scope JaCoCo and limit times taken
    generated_test_classes = _extract_test_class_names(exec_results)
    focal_classes = _extract_focal_class_names(exec_results)

    coverage = compute_coverage_if_available(str(jacoco_csv), str(project_root), 
                                             target_tests=generated_test_classes, 
                                             focal_classes=focal_classes)

    report = {
        "total_evaluated": len(exec_results),
        **stats,
        "codebleu": codebleu,
        "coverage": coverage,
    }

    # 4) Compute Individual Metrics
    individual_metrics = []
    for item in exec_results:
        test_id = item.get("pair_id")
        exec_info = item.get("execution_result", {})
        status = exec_info.get("status", "")
        focal_class = item.get("focal_class", "")
        focal_method = item.get("focal_method", "")
        
        test_metric = {
            "pair_id": test_id,
            "focal_class": focal_class,
            "focal_method": focal_method,
            "status": status,
        }

        # Calculate CodeBLEU specifically for this item if referenced
        if test_id in pairs:
            pred = item.get("final_test_code", "")
            ref = pairs[test_id].get("test_code", "")
            if pred and ref:
                try:
                    cb = calc_codebleu([[ref]], [pred], lang="java")
                    test_metric["codebleu"] = cb
                except Exception:
                    test_metric["codebleu"] = None
            else:
                test_metric["codebleu"] = None
        else:
            test_metric["codebleu"] = None

        individual_metrics.append(test_metric)

    # Write overall report
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
        
    # Write individual reports
    individual_output_path = output_path.parent / "evaluation_individual.json"
    with open(individual_output_path, "w", encoding="utf-8") as f:
        json.dump(individual_metrics, f, indent=2, ensure_ascii=False)

    logger.info(f"Finished Step 6 Evaluation. Results saved to {output_path.parent}")
    return report

# ==========================================
# 2. Data Loading & Alignment
# ==========================================
def load_data(path: Path) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def load_pairs(pairs_path: Path) -> Dict[Any, Dict]:
    pairs_dict = {}
    if pairs_path.exists():
        with open(pairs_path, "r", encoding="utf-8") as f:
            pairs_list = json.load(f)
            for idx, p in enumerate(pairs_list):
                pid = p.get("pair_id", idx)
                pairs_dict[pid] = p
    return pairs_dict

def align_predictions(exec_results: List[Dict], pairs: Dict[Any, Dict]) -> List[Dict[str, str]]:
    aligned = []
    for item in exec_results:
        test_id = item.get("pair_id")
        if test_id not in pairs:
            continue
        pred = item.get("final_test_code", "")
        ref  = pairs[test_id].get("test_code", "")
        aligned.append({"prediction": pred, "reference": ref})
    return aligned

# ==========================================
# 3. Classify Execution Results
# ==========================================
def classify_results(exec_results: List[Dict]) -> Dict[str, int]:
    stats = {"success_pass": 0, "fail_test": 0, "fail_compile": 0, "fail_execute": 0}

    for item in exec_results:
        exec_info = item.get("execution_result", {})
        status    = exec_info.get("status", "")
        output    = exec_info.get("error_output", "")

        if status in ("Success Pass", "SUCCESS"):
            stats["success_pass"] += 1
        elif status == "Fail Compile" or "COMPILATION ERROR" in output or "Compilation failure" in output:
            stats["fail_compile"] += 1
        elif status == "Fail Execute":
            if "<<< FAILURE!" in output or "Failures:" in output:
                stats["fail_test"] += 1
            else:
                stats["fail_execute"] += 1
        elif status == "Fail Test":
            stats["fail_test"] += 1
        else:
            stats["fail_execute"] += 1

    return stats

# ==========================================
# 4. Compute Metrics
# ==========================================
def compute_codebleu(aligned_data: List[Dict[str, str]]) -> Dict[str, float]:
    if not aligned_data:
        return {}
    preds = [x["prediction"] for x in aligned_data]
    refs  = [[x["reference"]] for x in aligned_data]
    try:
        return calc_codebleu(refs, preds, lang="java")
    except Exception as e:
        logger.error(f"Failed to calculate CodeBLEU: {e}")
        return {}

# ==========================================
# 5. Maven Helper
# ==========================================
def _run_mvn(
    goals,          # str (space-separated) or list[str]
    project_root: str,
    extra_args: list = None,
    timeout: int = 300,
) -> Tuple[bool, str]:
    """
    Run one or more Maven goals in project_root.
    Returns (success: bool, combined_output: str).
    """
    goal_list = goals.split() if isinstance(goals, str) else list(goals)

    # Use mvnw wrapper if it exists in the project root, else fallback to system mvn
    mvn_cmd = "mvn"
    mvnw_path = Path(project_root) / "mvnw"
    if mvnw_path.exists():
        mvn_cmd = str(mvnw_path.resolve())

    cmd = (
        [mvn_cmd]
        + goal_list
        + ["--batch-mode", "-Dsurefire.failIfNoSpecifiedTests=false"]
        + (extra_args or [])
    )

    logger.info(f"Running: {' '.join(cmd)}  (cwd={project_root})")
    try:
        result = subprocess.run(
            cmd,
            cwd=project_root,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        success = result.returncode == 0
        output  = result.stdout + result.stderr
        if not success:
            logger.warning(
                f"mvn {' '.join(goal_list)} exited with code {result.returncode}:\n"
                f"{output[-2000:]}"
            )
        return success, output
    except FileNotFoundError:
        return False, "mvn not found in PATH"
    except subprocess.TimeoutExpired:
        return False, f"mvn {' '.join(goal_list)} timed out after {timeout}s"
    except Exception as e:
        return False, str(e)

# ==========================================
# 6. JaCoCo Coverage
# ==========================================
def compute_coverage_if_available(
    csv_path: str,
    project_root: str = None,
    target_tests: List[str] = None,
    focal_classes: List[str] = None,
    timeout: int = 600,
) -> str:
    """
    Parse JaCoCo line / instruction / branch coverage from jacoco.csv.

    Root cause fix: `jacoco:report` alone produces an empty report when there
    is no .exec file. We run `mvn test jacoco:report` together so the execution
    data is collected first.
    """
    if not os.path.exists(csv_path):
        if project_root is None:
            return f"Report not found: {csv_path}. Provide project_root to auto-run JaCoCo."

        extra_args = ["-DskipTests=false"]
        if target_tests:
            test_list = ",".join(target_tests)
            extra_args.append(f"-Dtest={test_list}")
            logger.info(f"JaCoCo test executions scoped to: {test_list}")

        ok, out = _run_mvn(
            ["test", "jacoco:report"],
            project_root,
            extra_args=extra_args,
            timeout=timeout,
        )
        # jacoco:report can still write the CSV even when some tests fail (non-zero exit).
        # Only give up if the file truly did not appear.
        if not ok and not os.path.exists(csv_path):
            return f"JaCoCo failed (mvn test jacoco:report):\n{out[-800:]}"
        if not os.path.exists(csv_path):
            return f"JaCoCo ran but report still not found at: {csv_path}"
        if not ok:
            logger.warning("Some tests failed, but JaCoCo report was generated successfully.")

    total_line = covered_line = 0
    total_instr = covered_instr = 0
    total_branch = covered_branch = 0

    try:
        with open(csv_path, newline="", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                # If focal classes are provided, only aggregate coverage for those under-test code classes
                if focal_classes:
                    base_class = row.get("CLASS", "").split("$")[0]
                    if base_class not in focal_classes:
                        continue

                def _int(key: str) -> int:
                    return int(row.get(key, 0) or 0)
                total_line     += _int("LINE_MISSED")        + _int("LINE_COVERED")
                covered_line   += _int("LINE_COVERED")
                total_instr    += _int("INSTRUCTION_MISSED") + _int("INSTRUCTION_COVERED")
                covered_instr  += _int("INSTRUCTION_COVERED")
                total_branch   += _int("BRANCH_MISSED")      + _int("BRANCH_COVERED")
                covered_branch += _int("BRANCH_COVERED")

        parts = []
        if total_line > 0:
            parts.append(f"Line {covered_line/total_line*100:.1f}% ({covered_line}/{total_line})")
        if total_instr > 0:
            parts.append(f"Instruction {covered_instr/total_instr*100:.1f}% ({covered_instr}/{total_instr})")
        if total_branch > 0:
            parts.append(f"Branch {covered_branch/total_branch*100:.1f}% ({covered_branch}/{total_branch})")

        return " | ".join(parts) if parts else "0% (no data rows)"
    except Exception as e:
        return f"Error parsing JaCoCo report: {e}"

# ==========================================
# 7. Helper functions
# ==========================================
def _extract_test_class_names(exec_results: List[Dict]) -> List[str]:
    """
    Pull FQCNs from execution results to scope PITest's targetTests.
    Tries explicit fields first, then parses the generated source code.
    """
    names = []
    for item in exec_results:
        class_name = item.get("test_class_name") or item.get("test_class") or item.get("class_name")
        if class_name:
            names.append(class_name)
            continue

        code = item.get("final_test_code", "")
        cls_match = re.search(r"public\s+class\s+(\w+)", code)
        if cls_match:
            pkg_match = re.search(r"^package\s+([\w.]+)\s*;", code, re.MULTILINE)
            pkg = pkg_match.group(1) + "." if pkg_match else ""
            names.append(pkg + cls_match.group(1))

    return list(dict.fromkeys(names))  # deduplicate, preserve order

def _extract_focal_class_names(exec_results: List[Dict]) -> List[str]:
    """
    Extract the list of focal classes (under-test classes) being evaluated.
    """
    names = []
    for item in exec_results:
        fc = item.get("focal_class")
        if fc:
            names.append(fc)
    return list(dict.fromkeys(names))
