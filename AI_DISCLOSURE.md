# Use of AI-assisted development tools

Disclosed per section 7 of the assessment pack.

Claude Code (Anthropic) was used as a development assistant while building this
solution. No other AI tooling was used, and no AI service is called at runtime:
the classifier is deterministic (YAML account mappings, regular expressions and
a string-similarity fallback), with no model dependency, API key or network
requirement.

The design, the judgement calls and the final code are mine, and I am
responsible for the submitted solution.
