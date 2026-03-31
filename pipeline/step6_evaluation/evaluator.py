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

    # Calculate overall coverage over all stored individual JaCoCo XML files
    total_line = covered_line = 0
    total_instr = covered_instr = 0
    total_branch = covered_branch = 0
    
    # 4) Compute Individual Metrics
    individual_metrics = []
    
    for item in exec_results:
        test_id = item.get("pair_id")
        exec_info = item.get("execution_result", {})
        status = exec_info.get("status", "")
        focal_class = item.get("focal_class", "")
        focal_method = item.get("focal_method", "")
        
        method_cov = "0%"
        cov_key = f"{focal_class}.{focal_method}"
        jacoco_xml = exec_info.get("jacoco_xml_path")
        
        if jacoco_xml and os.path.exists(jacoco_xml):
            o_cov, m_cov_dict, (t_l, c_l, t_i, c_i, t_b, c_b) = compute_coverage_if_available(
                jacoco_xml, focal_classes=[focal_class]
            )
            total_line += t_l
            covered_line += c_l
            total_instr += t_i
            covered_instr += c_i
            total_branch += t_b
            covered_branch += c_b
            
            method_cov = m_cov_dict.get(cov_key, "0%")
            
        test_metric = {
            "pair_id": test_id,
            "focal_class": focal_class,
            "focal_method": focal_method,
            "status": status,
            "coverage": method_cov
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

    # Compute overall coverage string
    parts = []
    if total_line > 0:
        parts.append(f"Line {covered_line/total_line*100:.1f}% ({covered_line}/{total_line})")
    if total_instr > 0:
        parts.append(f"Instruction {covered_instr/total_instr*100:.1f}% ({covered_instr}/{total_instr})")
    if total_branch > 0:
        parts.append(f"Branch {covered_branch/total_branch*100:.1f}% ({covered_branch}/{total_branch})")
    coverage_overall = " | ".join(parts) if parts else "0% (no data rows)"

    report = {
        "total_evaluated": len(exec_results),
        **stats,
        "codebleu": codebleu,
        "coverage": coverage_overall,
    }

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
# 5. Maven Helper (Removed - now runs in Step 5 environment)
# ==========================================

import xml.etree.ElementTree as ET

# ==========================================
# 6. JaCoCo Coverage
# ==========================================
def compute_coverage_if_available(
    xml_path: str,
    focal_classes: List[str] = None
) -> Tuple[str, Dict[str, str], Tuple[int, int, int, int, int, int]]:
    """
    Parse JaCoCo line / instruction / branch coverage from jacoco.xml.
    Returns:
       (overall_coverage_string, dict_of_method_coverage, (total_line, covered_line, total_instr, covered_instr, total_branch, covered_branch))
    """
    if not os.path.exists(xml_path):
        return f"Report not found: {xml_path}.", {}, (0, 0, 0, 0, 0, 0)

    total_line = covered_line = 0
    total_instr = covered_instr = 0
    total_branch = covered_branch = 0
    
    method_coverage_dict = {}

    try:
        tree = ET.parse(xml_path)
        
        for package in tree.findall('package'):
            for cls in package.findall('class'):
                full_cls_name = cls.get('name')
                base_class = full_cls_name.split("/")[-1].split("$")[0]
                
                is_focal = focal_classes and base_class in focal_classes
                
                if not focal_classes or is_focal:
                    for counter in cls.findall('counter'):
                        ctype = counter.get('type')
                        c_covered = int(counter.get('covered', 0))
                        c_missed = int(counter.get('missed', 0))
                        
                        if ctype == 'INSTRUCTION':
                            total_instr += (c_covered + c_missed)
                            covered_instr += c_covered
                        elif ctype == 'BRANCH':
                            total_branch += (c_covered + c_missed)
                            covered_branch += c_covered
                        elif ctype == 'LINE':
                            total_line += (c_covered + c_missed)
                            covered_line += c_covered
                
                for method in cls.findall('method'):
                    m_name = method.get('name')
                    cov_key = f"{base_class}.{m_name}"
                    
                    t_l = c_l = t_i = c_i = t_b = c_b = 0
                    
                    for counter in method.findall('counter'):
                        ctype = counter.get('type')
                        c_covered = int(counter.get('covered', 0))
                        c_missed = int(counter.get('missed', 0))
                        c_tot = c_covered + c_missed
                        
                        if ctype == 'INSTRUCTION':
                            t_i = c_tot
                            c_i = c_covered
                        elif ctype == 'BRANCH':
                            t_b = c_tot
                            c_b = c_covered
                        elif ctype == 'LINE':
                            t_l = c_tot
                            c_l = c_covered
                            
                    if cov_key not in method_coverage_dict:
                        method_coverage_dict[cov_key] = {
                            "t_line": 0, "c_line": 0,
                            "t_instr": 0, "c_instr": 0,
                            "t_br": 0, "c_br": 0
                        }
                    method_coverage_dict[cov_key]["t_line"] += t_l
                    method_coverage_dict[cov_key]["c_line"] += c_l
                    method_coverage_dict[cov_key]["t_instr"] += t_i
                    method_coverage_dict[cov_key]["c_instr"] += c_i
                    method_coverage_dict[cov_key]["t_br"] += t_b
                    method_coverage_dict[cov_key]["c_br"] += c_b

        parts = []
        if total_line > 0:
            parts.append(f"Line {covered_line/total_line*100:.1f}% ({covered_line}/{total_line})")
        if total_instr > 0:
            parts.append(f"Instruction {covered_instr/total_instr*100:.1f}% ({covered_instr}/{total_instr})")
        if total_branch > 0:
            parts.append(f"Branch {covered_branch/total_branch*100:.1f}% ({covered_branch}/{total_branch})")

        overall_cov = " | ".join(parts) if parts else "0% (no data rows)"
        
        final_method_cov = {}
        for m_key, cov in method_coverage_dict.items():
            m_parts = []
            if cov["t_line"] > 0:
                m_parts.append(f"Line {cov['c_line']/cov['t_line']*100:.1f}% ({cov['c_line']}/{cov['t_line']})")
            if cov["t_instr"] > 0:
                m_parts.append(f"Instruction {cov['c_instr']/cov['t_instr']*100:.1f}% ({cov['c_instr']}/{cov['t_instr']})")
            if cov["t_br"] > 0:
                m_parts.append(f"Branch {cov['c_br']/cov['t_br']*100:.1f}% ({cov['c_br']}/{cov['t_br']})")
            final_method_cov[m_key] = " | ".join(m_parts) if m_parts else "0%"
            
        return overall_cov, final_method_cov, (total_line, covered_line, total_instr, covered_instr, total_branch, covered_branch)
    except Exception as e:
        return f"Error parsing JaCoCo report: {e}", {}, (0, 0, 0, 0, 0, 0)
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
