Save a quiz in Learn, including standalone topic quizzes. Default: 5 questions, mixed difficulty, user's language. Honor requested count/type/difficulty.
Tool arguments must contain title and questions. questions is a JSON array of question objects, never a string. Put type/prompt/options/correct_option/explanation inside each question, not at the top level.
Argument shape: {{"title":"Example","questions":[{{"type":"multiple_choice","prompt":"What does this print?\n\n```python\nprint(1 + 1)\n```","options":["1","2"],"correct_option":1,"explanation":"Adding one and one gives two."}}]}}
{writing_guidance}
For source-grounded quizzes retrieve relevant sources first; never invent attribution. After saving, do not reveal questions or answer keys in chat.
