import json
import logging
import os
import subprocess
import shutil
import concurrent.futures
import threading
from pathlib import Path
from typing import List, Dict, Any, Tuple

from pipeline.utils import get_logger
from pipeline.step4_test_generation.generator import call_llm

logger = get_logger("step5_executor")

MAX_REPAIR_LOOPS = 3

class ExecutionResult:
    def __init__(self, status: str, message: str, error_output: str = ""):
        # Status can be: "Success Pass", "Fail Compile", "Fail Execute" (or "Fail Test" depending on junit output)
        self.status = status
        self.message = message
        self.error_output = error_output
        self.jacoco_xml_path = None
        
    def to_dict(self):
        d = {
            "status": self.status,
            "message": self.message,
            "error_output": self.error_output
        }
        if self.jacoco_xml_path:
            d["jacoco_xml_path"] = self.jacoco_xml_path
        return d

def find_test_file_path(project_root: Path, test_class_name: str) -> Path:
    """
    Finds the correct path to place the test file in the project's src/test/java directory.
    This assumes a standard Maven layout.
    """
    test_dir = project_root / "src" / "test" / "java"
    
    # Try to find existing test file to get the exact package path
    for path in test_dir.rglob(f"{test_class_name}.java"):
        return path
        
    # If not found, default to root of test dir (or a default package like 'spark')
    # For spark-master, tests are usually in 'spark' package
    return test_dir / "spark" / f"{test_class_name}.java"

def write_test_code_to_file(test_file_path: Path, test_code: str):
    """Writes the generated test code to the project directory."""
    test_file_path.parent.mkdir(parents=True, exist_ok=True)
    with open(test_file_path, "w", encoding="utf-8") as f:
        f.write(test_code)

def run_maven_test(project_root: Path, test_class_name: str) -> ExecutionResult:
    """
    Runs `mvn clean test jacoco:report -Dtest=TestClass` in the project directory.
    Returns the execution result containing status and error output.
    """
    # Force compilation of tests to ensure the new file is picked up
    # -DfailIfNoTests=false prevents failure if the test class is somehow not found by surefire immediately
    cmd = ["mvn", "clean", "test", "jacoco:report", f"-Dtest={test_class_name}", "-DfailIfNoTests=false"]
    
    try:
        # Run maven command
        result = subprocess.run(
            cmd,
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=180 # Increased timeout to 3 minutes
        )
        
        stdout = result.stdout
        stderr = result.stderr
        full_output = stdout + "\n" + stderr
        
        if result.returncode == 0:
            return ExecutionResult("Success Pass", "Test passed successfully.")
            
        stdout = result.stdout
        stderr = result.stderr
        
        # Determine if it's a compilation error or execution/assertion failure
        if result.returncode != 0:
            if "COMPILATION ERROR" in stdout or "Compilation failure" in stdout or "compiler-plugin" in stdout and "FAILURE" in stdout:
                # Extract compilation errors more intelligently
                error_lines = [line for line in stdout.split('\n') if "[ERROR]" in line and ("/" in line or ".java" in line)]
                
                error_msg = "\n".join(error_lines)
                if not error_msg.strip():
                     # Fallback: capture last 100 lines of stdout if regex extraction failed
                     error_msg = "\n".join(stdout.split('\n')[-100:])
                     
                return ExecutionResult("Fail Compile", "Compilation failed.", error_output=error_msg)
            else:
                # Execution or Assertion failure
                error_msg = ""
                lines = stdout.split('\n')
                failure_lines = []
                capture = False
                for line in lines:
                    if "<<< FAILURE!" in line or "<<< ERROR!" in line or "Failures: " in line:
                        capture = True
                    if capture:
                        failure_lines.append(line)
                
                if failure_lines:
                     error_msg = "\n".join(failure_lines)
                else:
                     error_msg = "\n".join(lines[-50:])
                    
                return ExecutionResult("Fail Execute", "Test failed during execution or assertions.", error_output=error_msg)
            
    except subprocess.TimeoutExpired:
        return ExecutionResult("Fail Execute", "Test execution timed out (possible infinite loop).")
    except Exception as e:
        return ExecutionResult("Fail Execute", f"Failed to run maven command: {str(e)}")

def build_repair_prompt(original_prompt: str, test_code: str, error_result: ExecutionResult) -> str:
    """
    Constructs a prompt asking the LLM to fix the failing test.
    """
    prompt = [
        "You are an expert Java developer and testing engineer.",
        "You previously generated a JUnit test class based on the following requirements and context:",
        "================ ORIGINAL CONTEXT ================",
        original_prompt.replace("You are an expert Java developer and testing engineer.\n", "", 1), # strip repeated system prompt
        "==================================================",
        "",
        "However, the generated test code failed when executed. Here is the generated code:",
        "```java",
        test_code,
        "```",
        "",
        f"### Error Type: {error_result.status}",
        "### Error Details:",
        "```text",
        error_result.error_output,
        "```",
        "",
        "### Task:",
        "1. Analyze the root cause of the error based on the error details and the provided context.",
        "2. Rewrite the complete Java test class to fix the issues.",
        "3. Ensure you still satisfy the original test intents.",
        "4. Only output the corrected valid Java code block. Do not output markdown text outside the code block."
    ]
    
    return "\n".join(prompt)

def backup_original_test(test_file_path: Path) -> str:
    """Backs up existing test file if it exists, returns original content or empty string."""
    if test_file_path.exists():
        with open(test_file_path, "r", encoding="utf-8") as f:
            return f.read()
    return ""

def restore_original_test(test_file_path: Path, original_content: str):
    """Restores the original test file."""
    if original_content:
        with open(test_file_path, "w", encoding="utf-8") as f:
            f.write(original_content)
    elif test_file_path.exists():
        test_file_path.unlink()

def copy_project_for_worker(project_root: Path, worker_id: int) -> Path:
    """
    Creates an isolated copy of the project for a worker.
    Returns the path to the isolated project.
    """
    # Use a temp directory for workers, e.g., /tmp/test_workers/worker_N
    # Or sibling directories: project_root_worker_N
    worker_project_path = project_root.parent / f"{project_root.name}_worker_{worker_id}"
    
    if worker_project_path.exists():
        shutil.rmtree(worker_project_path)
        
    # Copy project
    # Using shutil.copytree with ignore to skip target/ and other build artifacts if possible to speed up
    # We ignore 'target' to force fresh compilation in the worker
    shutil.copytree(project_root, worker_project_path, ignore=shutil.ignore_patterns('target', '.git', '.idea'))
    
    return worker_project_path

def cleanup_worker_project(worker_project_path: Path):
    """Removes the isolated project copy."""
    if worker_project_path.exists():
        shutil.rmtree(worker_project_path)

def process_record(args):
    """
    Worker function to process a single test record in an isolated environment.
    """
    if len(args) == 6:
        record, project_root, i, total_records, worker_id, output_dir = args
    else:
        record, project_root, i, total_records, worker_id = args
        output_dir = Path("data/processed/spark-master")
    
    # Create isolated project copy for this worker (or reuse if persistent worker model)
    # NOTE: Copying project per record is too slow. We should copy per WORKER.
    
    # We need to make sure the target/test-classes directory is cleaned for the specific test class
    # to force recompilation. 
    # But since we are in an isolated environment, maybe we can just run clean once at start?
    # Or just rely on Maven.
    
    pair_id = record.get("pair_id")
    test_class = record.get("test_class")
    test_code = record.get("generated_test_code", "")
    original_prompt = record.get("prompt_used", "")
    
    # project_root here should be the ISOLATED path passed from the main loop
    worker_project_root = project_root 
    
    if not test_class or not test_code or "Generated Test Case Content Here" in test_code:
        logger.warning("Pair %s: Invalid or empty test code. Skipping.", pair_id)
        record["execution_result"] = {"status": "Skipped", "message": "Invalid test code generated."}
        return record
        
    test_file_path = find_test_file_path(worker_project_root, test_class)
    original_content = backup_original_test(test_file_path)
    
    current_code = test_code
    loop_count = 0
    final_result = None
    
    while loop_count <= MAX_REPAIR_LOOPS:
        # Check for invalid/empty generation before testing
        if current_code.strip() == "// Failed to generate test after retries" or "@Test" not in current_code:
            exec_res = ExecutionResult("Fail Compile", "Invalid test code generated (missing @Test or generation failed).")
            # If the initial generation failed completely, we skip repair loops
            if current_code.strip() == "// Failed to generate test after retries" or loop_count == MAX_REPAIR_LOOPS:
                final_result = exec_res
                break
        else:
            # 1. Write current code to project
            write_test_code_to_file(test_file_path, current_code)
            
            # 2. Execute maven test
            exec_res = run_maven_test(worker_project_root, test_class)
        
        if exec_res.status == "Success Pass":
            final_result = exec_res
            
            # Copy jacoco.xml to processed directory for this specific pair
            jacoco_src = worker_project_root / "target" / "site" / "jacoco" / "jacoco.xml"
            if jacoco_src.exists():
                jacoco_dir = output_dir / "jacoco_reports"
                jacoco_dir.mkdir(parents=True, exist_ok=True)
                jacoco_dest = jacoco_dir / f"jacoco_{pair_id}.xml"
                shutil.copy(jacoco_src, jacoco_dest)
                exec_res.jacoco_xml_path = str(jacoco_dest)
            
            break
            
        if loop_count < MAX_REPAIR_LOOPS:
            # 3. Build repair prompt
            repair_prompt = build_repair_prompt(original_prompt, current_code, exec_res)
            
            # 4. Call LLM for repair
            current_code = call_llm(repair_prompt)
        else:
            final_result = exec_res
            
        loop_count += 1
        
    # Restore original test file
    restore_original_test(test_file_path, original_content)
    
    # Save results
    record["final_test_code"] = current_code
    record["repair_loops"] = loop_count if final_result and final_result.status == "Success Pass" else MAX_REPAIR_LOOPS
    record["execution_result"] = final_result.to_dict() if final_result else exec_res.to_dict()
    
    return record

def run(generated_tests_path: Path, project_root: Path, output_path: Path, limit: int = 0) -> List[Dict[str, Any]]:
    """
    Executes Step 5: Test Execution and Self-Correction loop.
    Uses parallel execution with project isolation.
    """
    if not generated_tests_path.exists():
        logger.error("Generated tests file not found: %s", generated_tests_path)
        return None
        
    with open(generated_tests_path, "r", encoding="utf-8") as f:
        records = json.load(f)
        
    if limit > 0:
        records = records[:limit]
        logger.info("Limiting execution to %d records for testing.", limit)
        
    logger.info("Loaded %d generated test records for execution.", len(records))
    
    results = []
    total_records = len(records)
    completed_count = 0
    lock = threading.Lock()
    
    # Configure Parallel Execution
    MAX_WORKERS = 4
    logger.info(f"Preparing {MAX_WORKERS} isolated project environments...")
    
    # Create isolated project copies
    worker_envs = []
    try:
        for i in range(MAX_WORKERS):
            logger.info(f"Copying project for worker {i+1}...")
            env_path = copy_project_for_worker(project_root, i+1)
            worker_envs.append(env_path)
            
        logger.info("Isolation complete. Starting parallel execution...")
        
        with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
            futures = []
            
            # Distribute tasks to workers via round-robin or queue
            # Since ThreadPoolExecutor manages threads, we just submit tasks.
            # But we need to tell the task WHICH environment to use.
            # We can't easily know which thread picks up which task in a simple submit loop.
            # Solution: We can use a Queue of environment paths.
            
            import queue
            env_queue = queue.Queue()
            for env in worker_envs:
                env_queue.put(env)
                
            def worker_wrapper(record, i, total_records):
                # Acquire an environment
                env_path = env_queue.get()
                try:
                    return process_record((record, env_path, i, total_records, 0, output_path.parent))
                finally:
                    # Release environment back to queue
                    env_queue.task_done()
                    env_queue.put(env_path)

            futures = [
                executor.submit(worker_wrapper, record, i, total_records)
                for i, record in enumerate(records)
            ]
            
            for future in concurrent.futures.as_completed(futures):
                try:
                    result = future.result()
                    results.append(result)
                    
                    with lock:
                        completed_count += 1
                        percent = (completed_count / total_records) * 100
                        bar_length = 30
                        filled_length = int(bar_length * completed_count // total_records)
                        bar = '█' * filled_length + '-' * (bar_length - filled_length)
                        print(f'\rProgress: |{bar}| {percent:.1f}% ({completed_count}/{total_records})', end='', flush=True)
                except Exception as e:
                    logger.error(f"Error executing test: {e}")
                    
    finally:
        # Cleanup isolated environments
        print()
        logger.info("Cleaning up isolated environments...")
        for env_path in worker_envs:
            cleanup_worker_project(env_path)
    
    # Sort results by pair_id to maintain order
    results.sort(key=lambda x: x.get("pair_id", 0))
        
    # Write to output file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
        
    logger.info("Saved %d execution results to %s", len(results), output_path)
    
    # Print Summary Metrics
    success_pass = sum(1 for r in results if r.get("execution_result", {}).get("status") == "Success Pass")
    fail_compile = sum(1 for r in results if r.get("execution_result", {}).get("status") == "Fail Compile")
    fail_execute = sum(1 for r in results if r.get("execution_result", {}).get("status") == "Fail Execute")
    
    logger.info("=== Execution Summary ===")
    logger.info("Total Tested : %d", len(results))
    logger.info("Success Pass : %d", success_pass)
    logger.info("Fail Compile : %d", fail_compile)
    logger.info("Fail Execute : %d", fail_execute)
    
    return results
