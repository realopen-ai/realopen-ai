"""
AI Agent framework for RealOpen-AI.

The agent sits between the API layer and Ollama. It:
1. Receives the user message (with optional images/documents)
2. Decides which tools to call (if any)
3. Executes tools and feeds results back to the LLM
4. Streams the final response back to the API layer

Tool system:
- Each tool has a name, description, and execute() method
- The LLM decides which tools to call via structured output
- Tools return results that get appended to the conversation context
"""
