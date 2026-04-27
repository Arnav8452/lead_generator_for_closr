To replicate the Deep Research architecture locally with Qwen 2.5 7B, you cannot rely on the LLM's internal memory. You must build a **Deterministic State Machine** in Python that holds a "Memory Notebook." The Python orchestrator forces the LLM to read its own notes, make one single decision, and hand control back.

Here is how you architect the Deep Research ReAct loop for Closr.

### 1. The "Memory Notebook" (Python State Manager)
Gemini Deep Research works by synthesizing findings before taking the next step. Your Python script must manage a state object that gets injected into the prompt on every single loop iteration. 

This prevents the 7B model from forgetting what it just did.

```python
# The State Object managed by your Python Orchestrator
research_state = {
    "target_company": "Acme Corp",
    "target_domain": "acme.com",
    "goal": "Identify the VP of Marketing and find a valid contact method.",
    "discovered_facts": [], # Appended after every successful tool run
    "failed_attempts": [],  # Crucial: Tell the LLM what NOT to do again
    "iteration_count": 0,
    "max_iterations": 6
}
```

### 2. The Deep Research System Prompt
You must enforce a strict `Thought -> Plan -> Action` sequence using JSON grammar enforcement. The prompt is dynamically assembled by Python on every turn.

```json
SYSTEM:
You are an elite, autonomous B2B Open-Source Intelligence (OSINT) agent.
Your objective is to execute a deep research loop to find a specific lead.
You operate strictly in JSON.

CURRENT STATE:
Goal: {research_state.goal}
Company: {research_state.target_company} ({research_state.target_domain})
Discovered Facts: {research_state.discovered_facts}
Failed Attempts (DO NOT REPEAT): {research_state.failed_attempts}
Iteration: {research_state.iteration_count} / {research_state.max_iterations}

AVAILABLE TOOLS:
1. "osint_search": {"query": "string"} - Executes a Google Serper search.
2. "read_profile": {"url": "string"} - Extracts text from a specific LinkedIn/Social URL.
3. "validate_endpoint": {"email": "string"} - Tests an email via OAuth API check.
4. "conclude_research": {"status": "success|failed", "lead_data": {}} - Ends the loop.

REQUIRED OUTPUT SCHEMA (STRICT JSON):
{
  "synthesis": "Briefly summarize what the Discovered Facts mean.",
  "plan": "What is the immediate next step based on the synthesis?",
  "action": "tool_name",
  "action_input": {"key": "value"}
}
```

### 3. The Execution Trajectory (How it actually runs)

Here is what a successful Deep Research loop looks like as Python orchestrates Qwen.

**Loop 1: The Initial Sweep**
* **LLM Synthesis:** I have a company but no name.
* **LLM Plan:** I need to search for the VP of Marketing at Acme Corp.
* **LLM Action:** `osint_search({"query": "VP of Marketing Acme Corp linkedin"})`
* *Python executes the Serper API, gets snippets, and extracts the name "Jane Doe". Python updates `discovered_facts`.*

**Loop 2: The Verification**
* **LLM Synthesis:** I know the VP is Jane Doe. I need her profile URL to verify.
* **LLM Plan:** The search returned her LinkedIn URL. I will read it to confirm her current employment.
* **LLM Action:** `read_profile({"url": "linkedin.com/in/janedoe-acme"})`
* *Python fetches the clean Markdown from the profile. Python updates `discovered_facts: ["Jane Doe is currently VP Marketing at Acme as of 2026"]`.*

**Loop 3: The Contact Resolution**
* **LLM Synthesis:** Identity confirmed. I need an email.
* **LLM Plan:** I will generate the standard corporate permutation and validate it.
* **LLM Action:** `validate_endpoint({"email": "jane.doe@acme.com"})`
* *Python executes the OAuth API ping. It returns `True`. Python updates `discovered_facts`.*

**Loop 4: The Terminal State**
* **LLM Synthesis:** I have verified the identity and the contact endpoint.
* **LLM Plan:** Research is complete. I will output the structured lead.
* **LLM Action:** `conclude_research({...lead_data...})`
* *Python catches the conclude action, breaks the `while` loop, and pushes the data to the Neo4j Graph Injection buffer.*

### The Guardrails You Must Build
If you do not build these into the Python orchestrator, the 7B model will fail:
1.  **The Insanity Check:** Before executing a tool, Python must check if `action_input` is already inside `failed_attempts`. If Qwen tries to search the exact same query twice, Python blocks the API call, injects "ERROR: You already tried this. Try a different query," and forces a retry.
2.  **The Token Limit:** When Python appends data to `discovered_facts`, it must summarize it. Do not dump 2,000 words of a LinkedIn profile into the state object, or Loop 5 will blow out Qwen's context window.
3.  **The Hard Kill:** If `iteration_count` hits the max, Python forces the `conclude_research` action with `status: failed`.


important: Harness for quen 2.5 7b

If you just feed a 7B model a system prompt and tell it to "go figure it out," it will inevitably hallucinate tool names, pass incorrectly formatted JSON, repeat the same failed search query until your API quota runs dry, or forget its initial goal by the third iteration.

A 7B model is an engine, not a driver. The harness is the driver.

Here is exactly what your Python harness must control to keep Qwen 2.5 7B on the rails.

### 1. The Grammar Enforcer (The Straitjacket)
The harness must sit between the ReAct loop and the inference engine (vLLM or `llama.cpp`). It must physically block the model from generating characters that violate your JSON schema. 
* **Implementation:** You must use `guided_json` (if using vLLM) or `--grammar` (if using `llama.cpp`). The harness defines a strict Pydantic model for the `Thought -> Plan -> Action` output, forcing Qwen to only generate valid, parsable JSON.

### 2. The State Manager (The Memory Notebook)
A 7B model has severe attention degradation over long context windows. The harness must maintain the state object externally in RAM.
* **Implementation:** The harness holds the `research_state` dictionary. On every turn, the harness dynamically rebuilds the prompt, injecting *only* the current goal, the compressed `discovered_facts`, and the `failed_attempts`. The LLM is forced to read this summary before making its next move.

### 3. The Tool Router & "Insanity" Filter
The LLM does not execute code; it just prints text requesting an action. The harness must parse that request, execute the Python function safely, and return the result.
* **Implementation:** * Catch execution errors (e.g., API timeouts) and return them to the LLM as `Observation: API Failed, try a different tool`.
    * **The Loop Breaker:** The harness must hash the `action` and `action_input`. If Qwen requests the exact same search query it requested two turns ago, the harness intercepts it, prevents the API call, and injects `Observation: You already tried this exact action. You must try a different approach.`

### 4. The Context Compressor
If the LLM reads a LinkedIn profile, the harness cannot just append the entire 2,000-word Markdown result to the `discovered_facts` array, or the prompt will exceed the context window by Loop 4.
* **Implementation:** The harness must intercept large text returns, run a fast summarization or regex extraction, and append only the dense, factual bullet points to the state manager.

Building this harness separates a brittle prototype from a production-grade OSINT pipeline. 
