Use Markdown in prompt, options, answer and explanation. Put code snippets in fenced code blocks with a language tag and real newlines/indentation; use inline backticks only for short identifiers or expressions. The prompt contains only the question and necessary context/code, never the answer choices or answer key. Put all choices only in options, without A)/B) labels. One concept per question; plausible distinct options, concise expected answers and explanations.
One options entry is one independent answer. Three alternatives need three strings, e.g. ["red","green","blue"], not one newline-joined string. Do not include another choice list inside an entry. Multiline code/output answers may use fenced blocks. When repairing choices, rebuild the array and update correct_option to match its new zero-based index.
Each multiple-choice question must include its own options array and exactly one correct_option index; never ask to select multiple answers. Complete argument example (one question shown; generate the requested count):

```json
{{"title":"Python basics","questions":[{{
  "type":"multiple_choice",
  "prompt":"What is `len([1, 2])`?",
  "options":["1","2","3"],
  "correct_option":1,
  "explanation":"The list contains two elements."
}}]}}
```

Code-block example (the decoded prompt text):

What does this print?

```python
x = [1, 2]
print(len(x))
```

For that question, options are ["1", "2", "3"] and correct_option is 1. In JSON, encode line breaks once as \n, not \\n. The decoded text must have actual line breaks. Put closing fences on their own line; preserve valid code indentation. Do not flatten multiline programs into prose or semicolon-separated snippets. Keep literal escapes inside code strings unchanged.
