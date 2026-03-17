import json
import logging
import difflib
import urllib.request
import urllib.error
import time
import re
from pathlib import Path
from typing import List, Dict, Any

from pipeline.utils import get_logger
from pipeline.step4_test_generation.prompt_builder import build_prompt

logger = get_logger("step4_generator")

DEEPSEEK_API_KEY = "sk-fbfc3f7bed9f4d3e84e8ccff927c708b"
DEEPSEEK_API_URL = "https://api.deepseek.com/chat/completions"

def extract_java_code(text: str) -> str:
    """Extracts Java code from markdown blocks if present."""
    match = re.search(r'```java\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        return match.group(1)
    match = re.search(r'```\s*(.*?)\s*```', text, re.DOTALL)
    if match:
        return match.group(1)
    return text

def call_llm(prompt: str) -> str:
    """
    Calls the DeepSeek API to generate test code.
    """
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}"
    }
    data = {
        "model": "deepseek-chat",
        "messages": [
            {"role": "user", "content": prompt}
        ],
        "temperature": 0.2
    }
    
    req = urllib.request.Request(DEEPSEEK_API_URL, headers=headers, data=json.dumps(data).encode('utf-8'))
    
    max_retries = 3
    for attempt in range(max_retries):
        try:
            with urllib.request.urlopen(req, timeout=120) as response:
                result = json.loads(response.read().decode('utf-8'))
                content = result['choices'][0]['message']['content']
                return extract_java_code(content)
        except urllib.error.HTTPError as e:
            error_body = e.read().decode('utf-8')
            logger.error("HTTPError %s: %s - %s", e.code, e.reason, error_body)
            if e.code == 429:
                time.sleep(5 * (attempt + 1))
                continue
            return f"// API Error: {e.code} {e.reason}\n// {error_body}"
        except Exception as e:
            logger.error("Error calling LLM: %s", e)
            time.sleep(5 * (attempt + 1))
            
    return "// Failed to generate test after retries"

def compute_similarity(text1: str, text2: str) -> float:
    """
    Computes a simple textual similarity score between two strings.
    Uses Python's built-in difflib.SequenceMatcher.
    """
    if not text1 or not text2:
        return 0.0
    return difflib.SequenceMatcher(None, text1, text2).ratio()

def compute_intent_similarity(target_intents: List[Dict[str, Any]], candidate_intents: List[Dict[str, Any]]) -> float:
    """
    Computes a simple similarity score between two lists of intents based on intent types.
    """
    if not target_intents or not candidate_intents:
        return 0.0
        
    target_types = set(i.get("intent_type") for i in target_intents)
    candidate_types = set(i.get("intent_type") for i in candidate_intents)
    
    if not target_types or not candidate_types:
        return 0.0
        
    intersection = target_types.intersection(candidate_types)
    union = target_types.union(candidate_types)
    
    # Jaccard similarity for intent types
    return len(intersection) / len(union)

def get_similar_tests(pairs: List[Dict[str, Any]], target_focal_code: str, target_intents: List[Dict[str, Any]], exclude_pair_id: int, intents_data: List[Dict[str, Any]] = None) -> List[str]:
    """
    Find similar test cases based on a combined score of:
    1. Textual similarity of their corresponding focal methods (weight: 0.5)
    2. Intent similarity (Jaccard similarity of intent types) (weight: 0.5)
    
    We limit to max 2 examples to save prompt context length.
    """
    if not target_focal_code:
        return []

    # Create a mapping of pair_id to intents for fast lookup
    pair_intents_map = {}
    if intents_data:
        for record in intents_data:
            pid = record.get("pair_id")
            if pid is not None:
                pair_intents_map[pid] = record.get("intents", [])

    # Calculate similarity scores for all valid pairs
    scored_pairs = []
    for idx, pair in enumerate(pairs):
        if idx == exclude_pair_id:
            continue
            
        candidate_focal_code = pair.get("focal_code", "")
        test_code = pair.get("test_code", "")
        
        if not candidate_focal_code or not test_code:
            continue
            
        # 1. Code textual similarity
        code_score = compute_similarity(target_focal_code, candidate_focal_code)
        
        # 2. Intent similarity
        intent_score = 0.0
        candidate_intents = pair_intents_map.get(idx, [])
        if target_intents and candidate_intents:
            intent_score = compute_intent_similarity(target_intents, candidate_intents)
            
        # Combined score
        final_score = (code_score * 0.5) + (intent_score * 0.5)
        
        scored_pairs.append((final_score, test_code))
        
    # Sort by similarity score in descending order
    scored_pairs.sort(key=lambda x: x[0], reverse=True)
    
    # Return top 2 similar test codes
    similar = [test_code for score, test_code in scored_pairs[:2]]
    return similar

def run(intents_path: Path, pairs_path: Path, output_path: Path, limit: int = 0) -> List[Dict[str, Any]]:
    """
    Executes Step 4: Test Case Generation.
    Reads intents, builds complete context prompt, generates tests via LLM, and saves.
    """
    if not intents_path.exists():
        logger.error("Intents file not found: %s", intents_path)
        return None
        
    if not pairs_path.exists():
        logger.error("Pairs file not found: %s", pairs_path)
        return None

    with open(intents_path, "r", encoding="utf-8") as f:
        intents_data = json.load(f)
        
    with open(pairs_path, "r", encoding="utf-8") as f:
        pairs_data = json.load(f)
        
    if limit > 0:
        intents_data = intents_data[:limit]
        logger.info("Limiting execution to %d records for testing.", limit)
        
    logger.info("Loaded %d intent records and %d pairs for context.", len(intents_data), len(pairs_data))
    
    generated_records = []
    
    for i, intent_record in enumerate(intents_data):
        pair_id = intent_record.get("pair_id", i)
        test_class = intent_record.get("test_class", "")
        focal_code = intent_record.get("context_code", {}).get("focal_code", "")
        target_intents = intent_record.get("intents", [])
        
        # 1. Retrieve similar test cases as context based on textual similarity of focal methods and intent similarity
        similar_tests = get_similar_tests(pairs_data, focal_code, target_intents, pair_id, intents_data)
        
        # 2. Build the comprehensive prompt
        prompt = build_prompt(intent_record, similar_tests)
        
        logger.debug("Generated prompt for pair_id %d (%s.%s)", 
                     pair_id, intent_record["focal_class"], intent_record["focal_method"])
                     
        # 3. Call LLM to generate the code
        # NOTE: For real usage, you may want to add retries, timeout, and parsing logic here
        generated_code = call_llm(prompt)
        
        # 4. Save the generated test code along with metadata
        generated_record = {
            "pair_id": pair_id,
            "focal_class": intent_record["focal_class"],
            "focal_method": intent_record["focal_method"],
            "test_class": test_class,
            "generated_test_code": generated_code,
            "prompt_used": prompt
        }
        generated_records.append(generated_record)
        
    # Write to output file
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(generated_records, f, indent=2, ensure_ascii=False)
        
    logger.info("Saved %d generated test records to %s", len(generated_records), output_path)
    return generated_records

if __name__ == "__main__":
    # Simple test execution
    base_dir = Path(__file__).resolve().parent.parent.parent
    data_dir = base_dir / "data" / "processed" / "spark-master"
    
    run(
        data_dir / "intents.json",
        data_dir / "pairs.json",
        data_dir / "generated_tests.json"
    )
