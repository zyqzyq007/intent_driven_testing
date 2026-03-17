"""
Step 5: Test Execution and Self-Correction
==========================================
Executes the generated JUnit test cases.
If a test fails (compilation or execution), gathers error context and uses LLM to repair it,
looping up to MAX_REPAIRS times.
"""
