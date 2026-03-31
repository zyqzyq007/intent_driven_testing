import json
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import List, Dict, Any

def get_project_dependencies(project_root: Path) -> Dict[str, str]:
    """
    Parses pom.xml to identify key testing libraries and their versions.
    Returns a dict like: {"junit": "4.12", "mockito": "1.10.19"}
    """
    pom_path = project_root / "pom.xml"
    if not pom_path.exists():
        # Fallback if no pom.xml found
        return {}
        
    deps = {}
    try:
        # Register namespace to parse maven pom properly
        namespaces = {'mvn': 'http://maven.apache.org/POM/4.0.0'}
        tree = ET.parse(pom_path)
        root = tree.getroot()
        
        # Helper to resolve properties like ${junit.version}
        properties = {}
        props_elem = root.find('mvn:properties', namespaces)
        if props_elem is not None:
            for prop in props_elem:
                properties[prop.tag.replace(f"{{{namespaces['mvn']}}}", "")] = prop.text

        def resolve_version(ver_str):
            if ver_str and ver_str.startswith("${") and ver_str.endswith("}"):
                prop_name = ver_str[2:-1]
                return properties.get(prop_name, ver_str)
            return ver_str

        # Scan dependencies
        # Note: This is a simple scan, it doesn't handle parent poms or dependency management fully
        for dep in root.findall(".//mvn:dependency", namespaces):
            group_id = dep.find('mvn:groupId', namespaces)
            artifact_id = dep.find('mvn:artifactId', namespaces)
            version = dep.find('mvn:version', namespaces)
            
            if group_id is not None and artifact_id is not None:
                g = group_id.text
                a = artifact_id.text
                v = resolve_version(version.text) if version is not None else "unknown"
                
                if "junit" in a:
                    deps["junit"] = v
                    deps["junit_major"] = v.split('.')[0] if v and v[0].isdigit() else "4"
                elif "mockito" in a:
                    deps["mockito"] = v
                elif "powermock" in a:
                    deps["powermock"] = v
                elif "jupiter" in a or "junit-platform" in a:
                    deps["junit_major"] = "5"
                    
    except Exception as e:
        print(f"Warning: Failed to parse pom.xml: {e}")
        
    return deps

def build_prompt(intent_record: Dict[str, Any], similar_tests: List[str] = None, project_root: Path = None, test_imports: List[str] = None) -> str:
    """
    Builds a complete and correct context prompt for the LLM to generate a test case.
    
    Args:
        intent_record: A single record from intents.json
        similar_tests: Optional list of similar test cases (source code strings) from pairs.json
                       to be used as few-shot examples.
        project_root: Path to the project root, used to detect dependencies from pom.xml
    
    Returns:
        The full string prompt to send to the LLM.
    """
    focal_class = intent_record["focal_class"]
    focal_method = intent_record["focal_method"]
    context_code = intent_record["context_code"]
    intents = intent_record["intents"]
    
    # Detect testing framework versions
    test_deps = get_project_dependencies(project_root) if project_root else {}
    junit_version = test_deps.get("junit", "4.12")
    junit_major = test_deps.get("junit_major", "4")
    mockito_version = test_deps.get("mockito", "1.10.19")
    
    # 1. Start with system instructions
    prompt = [
        "You are an expert Java developer and testing engineer.",
        "Your task is to generate a JUnit test class or methods for a given focal method based on specific test intents.",
        "The generated test MUST follow the structured Given-When-Then (GWT) constraints provided below.",
    ]
    
    # Dynamic Framework Constraints
    if junit_major == "5":
        prompt.append("Strictly use **JUnit 5** (`org.junit.jupiter.api.*`).")
        prompt.append("Use `@Test`, `@BeforeEach`, `@AfterEach`, `Assertions.assertEquals`.")
        if "mockito" in test_deps:
            prompt.append("Use `@ExtendWith(MockitoExtension.class)` for Mockito integration.")
    else:
        # Default to JUnit 4
        prompt.append(f"Strictly use **JUnit 4** (version {junit_version}) (`org.junit.*`).")
        prompt.append("Use `@Test`, `@Before`, `@After`, `Assert.assertEquals`.")
        prompt.append("Do NOT use JUnit 5 (`org.junit.jupiter.*`) imports.")
        
        if "powermock" in test_deps:
            prompt.append(f"Use PowerMock {test_deps['powermock']} with `@RunWith(PowerMockRunner.class)` if you need to mock static/private methods.")
        elif "mockito" in test_deps:
            prompt.append(f"Use Mockito {mockito_version} (`@RunWith(MockitoJUnitRunner.class)` or `MockitoAnnotations.initMocks(this)`).")
            if mockito_version.startswith("1."):
                 prompt.append("Note: This is an older Mockito 1.x version. Use `Matchers` instead of `ArgumentMatchers`.")

    prompt.append("Ensure the generated code is syntactically correct, imports necessary dependencies, and focuses purely on the requested intent.")
    prompt.append("")
    
    # Optional: hint from actual test imports
    if test_imports:
        prompt.append("### Recommended Imports")
        prompt.append("These imports were used in similar test cases and may be highly relevant:")
        prompt.append("```java")
        prompt.append("\n".join(test_imports))
        prompt.append("```")
        prompt.append("")
    
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
