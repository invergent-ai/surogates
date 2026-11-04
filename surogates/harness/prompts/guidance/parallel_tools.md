---
name: parallel_tools
description: Batch independent lookups.
applies_when: at least two of read_file / search_files / list_files / web_search / web_extract / skill_view / session_search are available
---
**Batch independent lookups.** One response can hold several tool calls; they run together. When reads or searches don't depend on each other (several files, several searches, a file and a web page), send them in one response. Writes, commands and questions to the user go one at a time.
