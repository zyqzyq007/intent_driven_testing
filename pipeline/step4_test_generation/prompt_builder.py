import json
from typing import List, Dict, Any

def build_prompt(intent_record: Dict[str, Any], similar_tests: List[str] = None) -> str:
    """
    Builds a complete and correct context prompt for the LLM to generate a test case.
    
    Args:
        intent_record: A single record from intents.json
        similar_tests: Optional list of similar test cases (source code strings) from pairs.json
                       to be used as few-shot examples.
    
    Returns:
        The full string prompt to send to the LLM.
    """
    focal_class = intent_record["focal_class"]
    focal_method = intent_record["focal_method"]
    context_code = intent_record["context_code"]
    intents = intent_record["intents"]
    
    # 1. Start with system instructions
    prompt = [
        "You are an expert Java developer and testing engineer.",
        "Your task is to generate a JUnit test class or methods for a given focal method based on specific test intents.",
        "The generated test MUST follow the structured Given-When-Then (GWT) constraints provided below.",
        "Ensure the generated code is syntactically correct, imports necessary dependencies, and focuses purely on the requested intent.",
        ""
    ]
    
    # 2. Add Code Context
    prompt.append(f"### Code Context: {focal_class}.{focal_method}")
    if context_code.get("focal_class_imports"):
        prompt.append("#### Imports:")
        prompt.append("```java")
        prompt.append(context_code["focal_class_imports"])
        prompt.append("```")
        
    if context_code.get("field_definitions"):
        prompt.append("#### Class Field Definitions:")
        prompt.append("```java")
        for field_name, field_def in context_code["field_definitions"].items():
            prompt.append(f"// {field_name}\n{field_def}")
        prompt.append("```")
        
    prompt.append("#### Focal Method:")
    prompt.append("```java")
    prompt.append(context_code["focal_code"])
    prompt.append("```")
    
    if context_code.get("related_method_codes"):
        prompt.append("#### Related Methods (for context):")
        prompt.append("```java")
        for method_name, method_code in context_code["related_method_codes"].items():
            prompt.append(f"// {method_name}")
            prompt.append(method_code)
        prompt.append("```")
    
    prompt.append("")
    
    # 3. Add Similar Tests (Few-Shot Examples)
    if similar_tests:
        prompt.append("### Similar Test Cases (Context / Examples)")
        prompt.append("Here are some existing test cases from the same context to show you the testing style/conventions:")
        for idx, test_code in enumerate(similar_tests):
            prompt.append(f"#### Example {idx + 1}:")
            prompt.append("```java")
            prompt.append(test_code)
            prompt.append("```")
        prompt.append("")

    # 4. Add Test Intents
    prompt.append("### Test Intents to Generate")
    prompt.append("Please generate one distinct test method for EACH of the following intents:")
    
    for idx, intent in enumerate(intents):
        prompt.append(f"#### Intent {idx + 1}: {intent['intent_type']}")
        
        # Format Given
        prompt.append("**[Given] Context/Precondition:**")
        given = intent["given"]
        for s in given.get("lifecycle_states", []): prompt.append(f"- State: {s}")
        for c in given.get("setup_calls", []): prompt.append(f"- Setup: {c}")
        for d in given.get("data_preconditions", []): prompt.append(f"- Data: {d}")
        if not (given.get("lifecycle_states") or given.get("setup_calls") or given.get("data_preconditions")):
            prompt.append("- (no specific preconditions)")
            
        # Format When
        prompt.append("**[When] Action/Trigger:**")
        when = intent["when"]
        prompt.append(f"- Call: {when.get('method_call', '')}")
        prompt.append(f"- Position: {when.get('call_position', '')}")
        for p in when.get("parameters", []): prompt.append(f"- Param: {p}")
            
        # Format Then
        prompt.append("**[Then] Expected/Effect:**")
        then = intent["then"]
        for s in then.get("state_changes", []): prompt.append(f"- State change: {s}")
        for d in then.get("data_effects", []): prompt.append(f"- Data effect: {d}")
        for ds in then.get("downstream_effects", []): prompt.append(f"- Downstream: {ds}")
        if not (then.get("state_changes") or then.get("data_effects") or then.get("downstream_effects")):
            prompt.append("- (return value / no side effects)")
            
        prompt.append("")
        
    # 5. Output instructions
    prompt.append("### Output Requirements")
    prompt.append("1. Provide the complete Java code for the test methods.")
    prompt.append(f"2. Wrap the test methods inside a test class named `{intent_record.get('test_class', focal_class + 'Test')}`.")
    prompt.append("3. Only output valid Java code block. Do not output markdown text outside the code block.")
    
    return "\n".join(prompt)
